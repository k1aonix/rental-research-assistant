"""Offline normalization and read-only source access. No crawler dependency."""
import re
from collections import defaultdict

from .common import DomainError, decode, digest, encode, now


QUALITY = {'success': 'success', 'empty': 'empty_confirmed', 'empty_confirmed': 'empty_confirmed',
           'partial': 'partial', 'failed': 'failed', 'not_attempted': 'not_collected', 'not_collected': 'not_collected'}


def quality(value):
    return QUALITY.get(value, 'not_collected')


def valid_note(value):
    return isinstance(value, str) and re.fullmatch(r'[a-f0-9]{24}', value) is not None


def parse_link(value):
    from urllib.parse import urlparse
    parsed = urlparse(value.strip())
    if parsed.scheme != 'https' or parsed.hostname not in ('www.xiaohongshu.com', 'xiaohongshu.com'):
        raise DomainError('INVALID_REQUEST', '录入失败：无法识别Note ID，请使用含ID的小红书完整链接。', 422)
    match = re.fullmatch(r'/(?:explore|discovery/item)/([a-f0-9]{24})/?', parsed.path)
    if not match:
        raise DomainError('INVALID_REQUEST', '录入失败：无法识别Note ID，不自动访问短链接或在线补采。', 422)
    return match[1]


class SourceImporter:
    def __init__(self, database):
        self.db = database

    @staticmethod
    def inspect(data):
        if not isinstance(data, dict) or not isinstance(data.get('notes'), list):
            return {'valid': False, 'errors': ['顶层必须是对象，且notes必须为数组'], 'warnings': [], 'counts': {}, 'fields': {}}
        required = ('notes', 'comments', 'search_runs', 'search_results', 'author_history', 'author_history_runs')
        problems = []
        for name in required:
            if not isinstance(data.get(name, []), list):
                problems.append(name + '必须为数组')
        if problems:
            return {'valid': False, 'errors': problems, 'warnings': [], 'counts': {}, 'fields': {}}
        if any(not isinstance(item, dict) for name in required for item in data.get(name, [])):
            return {'valid': False, 'errors': ['源集合中每项必须是对象'], 'warnings': [], 'counts': {}, 'fields': {}}
        notes = data.get('notes', [])
        ids = [item.get('note_id') for item in notes]
        for row in notes:
            if not isinstance(row.get('note_id'), str):
                problems.append('notes的note_id必须为字符串')
        for row in data.get('search_runs', []):
            if not isinstance(row.get('search_id'), str) or not isinstance(row.get('keyword'), str):
                problems.append('search_runs需要字符串search_id和keyword')
            returned = row.get('returned_count')
            if returned is not None and (type(returned) is not int or returned < 0):
                problems.append('returned_count必须为非负整数或null')
        for row in data.get('search_results', []):
            if not isinstance(row.get('search_id'), str) or type(row.get('position')) is not int:
                problems.append('搜索位置的search_id和position类型不正确')
            if row.get('note_id') is not None and not isinstance(row.get('note_id'), str):
                problems.append('搜索结果note_id必须为字符串或null')
        for row in data.get('comments', []):
            if not isinstance(row.get('note_id'), str) or not isinstance(row.get('comment_id'), str):
                problems.append('评论关联标识必须为字符串')
        for name in ('author_history', 'author_history_runs'):
            if any(not isinstance(row.get('author_id'), str) for row in data.get(name, [])):
                problems.append(name+'需要字符串author_id')
        if problems:
            return {'valid': False, 'errors': sorted(set(problems)), 'warnings': [], 'counts': {}, 'fields': {}}
        if len(ids) != len(set(ids)):
            problems.append('notes存在重复Note ID')
        if any(not valid_note(identifier) for identifier in ids):
            problems.append('notes含无效Note ID')
        search_ids = [item.get('search_id') for item in data.get('search_runs', [])]
        if len(search_ids) != len(set(search_ids)):
            problems.append('search_runs存在重复search_id')
        positions = set()
        for row in data.get('search_results', []):
            key = (row.get('search_id'), row.get('position'))
            if key in positions or key[0] not in search_ids or type(key[1]) is not int or key[1] <= 0:
                problems.append('搜索位置重复、无关联或位置不合法')
            positions.add(key)
        note_ids = set(ids)
        comment_ids = set()
        for row in data.get('comments', []):
            key = (row.get('note_id'), row.get('comment_id'))
            if row.get('note_id') not in note_ids or not row.get('comment_id') or key in comment_ids:
                problems.append('评论关联或唯一性不合法')
            comment_ids.add(key)
        warnings = []
        if not data.get('author_history_runs'):
            warnings.append('作者历史尚未采集，不能作为确认空历史')
        if any((run.get('returned_count') or 0) > sum(row.get('search_id') == run.get('search_id')
               for row in data.get('search_results', [])) for run in data.get('search_runs', [])):
            warnings.append('部分旧搜索位置缺失，不能通过完整尝试计数验收')
        return {'valid': not problems, 'errors': sorted(set(problems)), 'warnings': warnings,
                'counts': {name: len(data.get(name, [])) for name in required},
                'fields': {name: sorted({key for item in data.get(name, []) for key in item}) for name in required}}

    def import_data(self, data, manifest, version):
        if not isinstance(manifest, dict) or any(not isinstance(manifest.get(key, {}), dict)
                for key in ('coverage', 'search_mappings', 'communities')):
            raise DomainError('SOURCE_VALIDATION_FAILED', '来源映射必须为对象，范围、搜索映射和小区映射也必须为对象。', 422)
        inspected = self.inspect(data)
        if not inspected['valid']:
            raise DomainError('SOURCE_VALIDATION_FAILED', '源文件校验失败。', 422, errors=inspected['errors'])
        content_hash = digest({'data': data, 'manifest': manifest})
        existing = self.db.one('SELECT * FROM imports WHERE version=?', (version,))
        if existing:
            if decode(existing['source_manifest']).get('sha256') != content_hash:
                raise DomainError('IMPORT_VERSION_CONFLICT', '冻结版本不可覆盖，请使用新版本号。')
            return existing['import_id']
        bundle = {**manifest, 'sha256': content_hash, 'source_schema_version': data.get('schema_version'),
                  'quality_inspection': inspected, 'searches': {}}
        authors = {}
        histories = defaultdict(list)
        history_runs = {item['author_id']: item for item in data.get('author_history_runs', [])}
        for item in data.get('author_history', []):
            if len(histories[item['author_id']]) < 5:
                histories[item['author_id']].append({key: item.get(key) for key in ('note_id', 'title', 'original_url')})
        for note in data['notes']:
            if note.get('author_id'):
                authors.setdefault(note['author_id'], note.get('author_nickname'))
        with self.db.transaction() as connection:
            import_id = connection.execute('INSERT INTO imports(version,source_manifest,frozen_at) VALUES(?,?,?)',
                                           (version, '{}', now())).lastrowid
            author_keys = {}
            for identifier, nickname in authors.items():
                run = history_runs.get(identifier, {})
                status = quality(run.get('status'))
                if histories[identifier] and status == 'not_collected':
                    status = 'partial'
                author_keys[identifier] = connection.execute(
                    'INSERT INTO authors(import_id,author_id,nickname,profile_text,history_titles,quality) VALUES(?,?,?,?,?,?)',
                    (import_id, identifier, nickname, run.get('profile_text'), encode(histories[identifier]),
                     encode({'status': status, 'source_status': run.get('status'), 'error': run.get('error')}))).lastrowid
            post_keys = {}
            for note in data['notes']:
                q = {'original_url_present': bool(note.get('original_url')), 'body': quality(note.get('status')), 'primary_comments': quality(note.get('primary_comments_status', note.get('comments_status'))),
                     'replies': quality(note.get('replies_status')), 'source_status': note.get('status'),
                     'comments_quality_granularity': 'separate' if 'primary_comments_status' in note else 'aggregate_legacy'}
                refs = manifest.get('communities', {}).get(note['note_id'], note.get('community_refs') or [])
                post_keys[note['note_id']] = connection.execute(
                    'INSERT INTO posts(import_id,note_id,author_key,url,title,body,published_at,quality,community_refs) VALUES(?,?,?,?,?,?,?,?,?)',
                    (import_id, note['note_id'], author_keys.get(note.get('author_id')), note.get('original_url') or
                     'https://www.xiaohongshu.com/explore/' + note['note_id'], note.get('title'), note.get('body'),
                     note.get('published_at'), encode(q), encode(refs))).lastrowid
            counts = defaultdict(int)
            comment_keys, parents = {}, []
            for row in data.get('comments', []):
                note_id = row['note_id']
                if counts[note_id] >= 10:
                    continue
                counts[note_id] += 1
                key = connection.execute(
                    'INSERT INTO comments(post_key,comment_id,level,fetch_order,body) VALUES(?,?,?,?,?)',
                    (post_keys[note_id], row['comment_id'], 2 if row.get('parent_comment_id') else 1,
                     counts[note_id], row.get('body') or '')).lastrowid
                comment_keys[(note_id, row['comment_id'])] = key
                if row.get('parent_comment_id'):
                    parents.append((key, note_id, row['parent_comment_id']))
            for key, note_id, parent in parents:
                parent_key = comment_keys.get((note_id, parent))
                if parent_key:
                    connection.execute('UPDATE comments SET parent_key=? WHERE comment_key=?', (parent_key, key))
            search_keys = {}
            for order, run in enumerate(data.get('search_runs', []), 1):
                metadata = {**run, **manifest.get('search_mappings', {}).get(run['search_id'], {})}
                region = metadata.get('region') or ('near_target' if run.get('round_id') == 1 else
                         'near_metro' if run.get('round_id') == 2 else 'preference' if metadata.get('preference_text') else 'community')
                search_key = connection.execute('INSERT INTO searches(import_id,query_text,region,station_poi,source_order,quality) VALUES(?,?,?,?,?,?)',
                    (import_id, run['keyword'], region, metadata.get('station_poi'), order, quality(run.get('status')))).lastrowid
                search_keys[run['search_id']] = search_key
                bundle['searches'][str(search_key)] = metadata
            for row in data.get('search_results', []):
                connection.execute('INSERT INTO search_results(search_key,position,raw_note_id,raw_url,raw_title,raw_summary,quality) VALUES(?,?,?,?,?,?,?)',
                    (search_keys[row['search_id']], row['position'], row.get('note_id'), row.get('raw_url'), row.get('raw_title'),
                     row.get('raw_summary'), quality(row.get('status'))))
            connection.execute('UPDATE imports SET source_manifest=? WHERE import_id=?', (encode(bundle), import_id))
        return import_id


class SourceRepository:
    def __init__(self, database):
        self.db = database

    def post(self, import_id, note_id):
        row = self.db.one('SELECT * FROM posts WHERE import_id=? AND note_id=?', (import_id, note_id))
        if not row:
            raise DomainError('SOURCE_NOT_FOUND', '暂未找到可分析的帖子。', 404)
        row['quality'] = decode(row['quality'])
        row['community_refs'] = decode(row['community_refs'])
        return row

    def comments(self, post_key):
        return self.db.all('SELECT * FROM comments WHERE post_key=? ORDER BY fetch_order LIMIT 10', (post_key,))

    def author(self, author_key):
        row = self.db.one('SELECT * FROM authors WHERE author_key=?', (author_key,))
        if row:
            row['quality'] = decode(row['quality'])
            row['history_titles'] = decode(row['history_titles'])[:5]
        return row

    def station_coverage(self, import_id, stations):
        batch=self.db.one('SELECT * FROM imports WHERE import_id=?',(import_id,))
        if not batch:raise DomainError('SOURCE_UNAVAILABLE','冻结源批次不可用。',503)
        manifest=decode(batch['source_manifest'])
        # Author-only revisions preserve search data, but source-version references
        # must only carry across when the actual snapshot is still identical.
        ancestors={batch['version']:batch};lineage=batch;seen={import_id}
        while True:
            revision=decode(lineage['source_manifest']).get('author_repair',{})
            parent=revision.get('parent_import_id')
            if not revision.get('preserves_non_author_sources') or parent in seen:break
            lineage=self.db.one('SELECT * FROM imports WHERE import_id=?',(parent,))
            if not lineage:break
            seen.add(parent);ancestors[lineage['version']]=lineage
        def same_snapshot(row,meta,reference):
            ancestor=ancestors[reference['source_version']]
            if ancestor['import_id']==import_id:return True
            metadata=decode(ancestor['source_manifest']).get('searches',{})
            originals=[r for r in self.db.all('SELECT * FROM searches WHERE import_id=?',(ancestor['import_id'],))
                       if r['query_text']==row['query_text'] and metadata.get(str(r['search_key']),{}).get('search_id')==reference['search_id']]
            if len(originals)!=1:return False
            original=originals[0]
            fields=('query_text','region','station_poi','source_order','quality')
            if any(original[f]!=row[f] for f in fields) or metadata.get(str(original['search_key']),{})!=meta:return False
            sql='SELECT position,raw_note_id,raw_url,raw_title,raw_summary,quality FROM search_results WHERE search_key=? ORDER BY position'
            return self.db.all(sql,(original['search_key'],))==self.db.all(sql,(row['search_key'],))
        snapshots=self.db.all('SELECT * FROM searches WHERE import_id=? AND region=? ORDER BY source_order',(import_id,'near_metro'))
        bindings=[];owners={};search_owners={};issues=[];limits=[]
        for station in stations:
            canonical=station.get('canonical_station_id') or station.get('station_id') or station.get('poi_id')
            ids=set(station.get('poi_ids') or ([station['poi_id']] if station.get('poi_id') else []))
            for poi_id in ids:
                if poi_id in owners and owners[poi_id]!=canonical:
                    raise DomainError('SNAPSHOT_BINDING_AMBIGUOUS','站点POI同时绑定多个站体，未猜测关联。',503)
                owners[poi_id]=canonical
            references=[r for r in station.get('snapshot_refs',[]) if r.get('verified') is True and r.get('source_version') in ancestors]
            matched=[]
            for row in snapshots:
                meta=manifest.get('searches',{}).get(str(row['search_key']),{})
                explicit=False
                for ref in references:
                    if ref.get('search_id')==meta.get('search_id') and ref.get('keyword')==row['query_text']:
                        if same_snapshot(row,meta,ref):explicit=True
                        else:issues.append({'station_id':canonical,'station_name':station.get('name') or station.get('station_name') or canonical,'search_key':row['search_key'],'state':'binding_revision_mismatch','search_id':meta.get('search_id'),'keyword':row['query_text']})
                if row['station_poi'] not in ids and not explicit:continue
                if row['search_key'] in search_owners and search_owners[row['search_key']]!=canonical:
                    raise DomainError('SNAPSHOT_BINDING_AMBIGUOUS','同一搜索快照绑定多个不同站点。',503)
                search_owners[row['search_key']]=canonical
                positions=self.db.all('SELECT position FROM search_results WHERE search_key=? ORDER BY position',(row['search_key'],))
                returned=meta.get('returned_count')
                complete=(row['quality'] in ('success','empty_confirmed') and
                          [p['position'] for p in positions]==list(range(1,len(positions)+1)) and
                          (returned is None or type(returned) is int and returned==len(positions)) and
                          (bool(positions) or row['quality']=='empty_confirmed'))
                if complete:
                    matched.append(row)
                    if returned is None and positions:limits.append({'station_id':canonical,'station_name':station.get('name') or station.get('station_name') or canonical,'search_id':meta.get('search_id'),'saved_position_count':len(positions),'reason':'original_return_count_unrecorded'})
                else:issues.append({'station_id':canonical,'station_name':station.get('name') or station.get('station_name') or canonical,'search_key':row['search_key'],'state':'snapshot_incomplete','quality':row['quality'],'search_id':meta.get('search_id'),'keyword':row['query_text'],'returned_count':returned,'saved_position_count':len(positions)})
            bindings.append({'canonical_station_id':canonical,'poi_ids':sorted(ids),
                             'station_name':station.get('name') or station.get('station_name') or canonical,
                             'state':'covered' if matched else 'snapshot_not_collected',
                             'snapshot_binding':('empty_confirmed' if all(r['quality']=='empty_confirmed' for r in matched) else 'bound') if matched else 'not_collected',
                             'search_keys':[r['search_key'] for r in matched]})
        missing=[b['canonical_station_id'] for b in bindings if b['state']!='covered']
        return {'bindings':bindings,'covered_count':len(bindings)-len(missing),'missing_station_ids':missing,
                'selected_count':len(stations),'source_version':batch['version'],'verified_binding_versions':list(ancestors),
                'snapshot_issues':issues,'completeness_limits':limits,'state':'partial' if missing or issues else 'complete'}

    def search(self, import_id, region, stations=None, target=None):
        batch = self.db.one('SELECT * FROM imports WHERE import_id=?', (import_id,))
        if not batch:
            raise DomainError('SOURCE_UNAVAILABLE', '冻结源批次不可用。', 503, True)
        manifest = decode(batch['source_manifest'])
        snapshots = self.db.all('SELECT * FROM searches WHERE import_id=? AND region=? ORDER BY source_order', (import_id, region))
        if region == 'near_metro':
            coverage=self.station_coverage(import_id, stations or [])
            if stations and not coverage['covered_count']:
                raise DomainError('STATION_SNAPSHOT_NOT_COLLECTED', '当前冻结来源尚无可用且已核验绑定的站点快照；请先检查缺失范围。同版本修复后可继续原任务，更换来源须明确采用新版本，未当作零结果。',503,
                                  snapshot_coverage=coverage)
            ranks={key:i for i,binding in enumerate(coverage['bindings']) for key in binding['search_keys']}
            snapshots=[row for row in snapshots if row['search_key'] in ranks]
            snapshots.sort(key=lambda row:(ranks[row['search_key']],row['source_order']))
        if not snapshots:
            declared = manifest.get('coverage', {}).get(region)
            if declared == 'empty_confirmed' or (region == 'near_metro' and not stations):
                return []
            raise DomainError('SOURCE_COVERAGE_MISSING', '源库没有该发现范围或站点的搜索快照，不能当作正常零命中。', 503)
        valid = [row for row in snapshots if row['quality'] in ('success', 'empty_confirmed', 'partial')]
        if not valid:
            raise DomainError('SOURCE_UNAVAILABLE', '原始搜索快照读取失败。', 503, True)
        rows = []
        for snapshot in valid:
            metadata = manifest.get('searches', {}).get(str(snapshot['search_key']), {})
            scope=metadata.get('discovery_scope') or {}
            anchor=scope.get('anchor') or {}
            duration=anchor.get('walk_sec')
            scope_valid=(metadata.get('discovery_scope_verified') is True and scope.get('mode')=='walking'
                         and scope.get('limit_sec')==1200 and type(duration) is int and 0<duration<=1200
                         and anchor.get('poi_id') and anchor.get('location') and scope.get('evidence_file')
                         and scope.get('target_poi') and scope.get('target_location'))
            if target and (scope.get('target_poi')!=target.get('target_poi') or scope.get('target_location')!=target.get('location')):
                scope_valid=False
            if region == 'near_target' and not scope_valid:
                raise DomainError('SOURCE_COVERAGE_MISSING', '首轮20分钟步行发现范围尚无核验映射；需补齐真实范围及完整快照后重试。', 503, True,
                                  search_id=metadata.get('search_id'), keyword=snapshot['query_text'], required_scope='walking_1200sec')
            items = self.db.all('SELECT * FROM search_results WHERE search_key=? ORDER BY position', (snapshot['search_key'],))
            returned = metadata.get('returned_count')
            if type(returned) is int and (returned!=len(items) or [x['position'] for x in items]!=list(range(1,returned+1))):
                raise DomainError('SOURCE_SNAPSHOT_INCOMPLETE', '旧搜索快照遗漏原始位置，不能伪装成完整尝试流。', 503)
            if not items and snapshot['quality'] != 'empty_confirmed':
                raise DomainError('SOURCE_UNAVAILABLE', '搜索快照缺少结果，且未确认真实空集合。', 503)
            rows.extend(items)
        return rows

    def auxiliary(self, import_id, community, preference=None):
        # Runs only after the hard gate. Never filters evidence by rental classification.
        # Filtering happens in SQLite; only the selected <=3 complete posts enter the stage.
        ordering = "SELECT s.source_order FROM search_results sr JOIN searches s ON s.search_key=sr.search_key WHERE sr.raw_note_id=p.note_id AND s.import_id=p.import_id ORDER BY s.source_order,sr.position LIMIT 1"
        position = ordering.replace('SELECT s.source_order', 'SELECT sr.position')
        text = "(COALESCE(p.title,'')||char(10)||COALESCE(p.body,''))"
        criterion = '(instr(' + text + ",?)>0 OR EXISTS(SELECT 1 FROM json_each(p.community_refs) ref WHERE json_extract(ref.value,'$.name')=?))"
        args = [import_id, community, community]
        if preference:
            criterion += ' AND instr(' + text + ',?)>0'
            args.append(preference)
        candidates = self.db.all('SELECT p.* FROM posts p WHERE p.import_id=? AND ' + criterion +
                                  ' ORDER BY COALESCE((' + ordering + '),2147483647),COALESCE((' + position + '),2147483647),p.note_id LIMIT 3', args)
        ranked = []
        for post in candidates:
            post['quality'] = decode(post['quality'])
            post['community_refs'] = decode(post['community_refs'], [])
            ranked.append(post)
        return ranked
