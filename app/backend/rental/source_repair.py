"""Explicit author-only source revision; old batches and successful work remain intact."""
from datetime import datetime
from .common import DomainError, decode, digest, encode, now
from .source import valid_note, quality


def author_revision_path(db, previous, current):
    seen=set()
    while current!=previous and current not in seen:
        seen.add(current)
        row=db.one('SELECT source_manifest FROM imports WHERE import_id=?',(current,))
        revision=decode(row['source_manifest']).get('author_repair',{}) if row else {}
        if not revision.get('preserves_non_author_sources'):return False
        current=revision.get('parent_import_id')
    return current==previous


def repair_authors(db, profile_id, reports, version, apply=False):
    profile=db.one('SELECT * FROM profiles WHERE profile_id=?',(profile_id,))
    if not profile or profile['state']!='locked':raise DomainError('INVALID_STATE','需要当前已锁定画像。')
    if db.one("SELECT task_id FROM tasks WHERE state IN('queued','running','waiting_user')") or db.one("SELECT job_id FROM assistant_jobs WHERE state IN('queued','running')"):
        raise DomainError('REPAIR_BUSY','有未结束任务，请先结束后再明确采用补充来源。')
    if db.one('SELECT import_id FROM imports WHERE version=?',(version,)):raise DomainError('SOURCE_VERSION_EXISTS','补充版本已存在，不能覆盖。')
    old=profile['import_id'];batch=db.one('SELECT * FROM imports WHERE import_id=?',(old,));updates={}
    for report in reports:
        a=report.get('data',{});aid=a.get('author_id');titles=a.get('history_titles')
        if (report.get('source')!='live_author_profile' or not valid_note(aid) or aid in updates or
            a.get('quality') not in ('success','partial','empty_confirmed') or not isinstance(titles,list) or len(titles)>5):
            raise DomainError('INVALID_AUTHOR_REPAIR','作者补充结果不完整、重复或失败，未采用。')
        try:datetime.fromisoformat(report['fetched_at'])
        except (ValueError,KeyError,TypeError):raise DomainError('INVALID_AUTHOR_REPAIR','作者补充缺少实际采集时间。') from None
        for item in titles:
            if not isinstance(item,dict) or not valid_note(item.get('note_id')) or not isinstance(item.get('title'),str) or item.get('original_url')!='https://www.xiaohongshu.com/explore/'+item['note_id']:
                raise DomainError('INVALID_AUTHOR_REPAIR','作者历史标题关联不合法。')
        if a['quality']=='success' and (not titles or any(not t['title'].strip() for t in titles)):
            raise DomainError('INVALID_AUTHOR_REPAIR','成功作者来源不能是未经确认的空历史。')
        author=db.one('SELECT * FROM authors WHERE import_id=? AND author_id=?',(old,aid))
        if not author or decode(author['quality'])['status'] not in ('failed','not_collected'):
            raise DomainError('INVALID_AUTHOR_REPAIR','只允许补齐该冻结批次中缺失或失败的作者来源。')
        if db.one("SELECT s.step_id FROM steps s JOIN records r ON r.record_id=s.record_id JOIN posts p ON p.post_key=r.post_key WHERE r.profile_id=? AND p.author_key=? AND s.step_name='author' AND s.state='succeeded'",(profile_id,author['author_key'])):
            raise DomainError('INVALID_AUTHOR_REPAIR','该作者已有成功分析，不能以补充操作替换输入。')
        updates[aid]=report
    if not updates:raise DomainError('INVALID_AUTHOR_REPAIR','没有可采用的作者资料。')
    result={'profile_id':profile_id,'from_import_id':old,'version':version,'author_ids':list(updates),'applied':apply,'preserved_successful_steps':True}
    if not apply:return result
    with db.transaction() as c:
        manifest=decode(batch['source_manifest']);manifest['author_repair']={'parent_import_id':old,'parent_sha256':manifest.get('sha256'),
            'preserves_non_author_sources':True,'reports':{aid:digest(r) for aid,r in updates.items()},'adopted_at':now()}
        manifest['sha256']=digest({'parent':manifest['sha256'],'authors':updates});search_meta=manifest.get('searches',{});manifest['searches']={}
        new=c.execute('INSERT INTO imports(version,source_manifest,frozen_at) VALUES(?,?,?)',(version,encode(manifest),now())).lastrowid
        authors={};posts={};comments={}
        for a in db.all('SELECT * FROM authors WHERE import_id=?',(old,)):
            report=updates.get(a['author_id']);data=report['data'] if report else None
            values=[a[k] for k in ('nickname','profile_text','history_titles','quality')]
            if data:values=[data.get('nickname'),data.get('profile_text'),encode(data['history_titles']),encode({'status':quality(data['quality']),'source_status':data['quality'],'error':data.get('error'),'fetched_at':report['fetched_at']})]
            authors[a['author_key']]=c.execute('INSERT INTO authors(import_id,author_id,nickname,profile_text,history_titles,quality) VALUES(?,?,?,?,?,?)',(new,a['author_id'],*values)).lastrowid
        for p in db.all('SELECT * FROM posts WHERE import_id=?',(old,)):
            posts[p['post_key']]=c.execute('INSERT INTO posts(import_id,note_id,author_key,url,title,body,published_at,quality,community_refs) VALUES(?,?,?,?,?,?,?,?,?)',
                (new,p['note_id'],authors.get(p['author_key']),*[p[k] for k in ('url','title','body','published_at','quality','community_refs')])).lastrowid
        original_comments=db.all('SELECT c.* FROM comments c JOIN posts p ON p.post_key=c.post_key WHERE p.import_id=? ORDER BY c.comment_key',(old,))
        for row in original_comments:
            comments[row['comment_key']]=c.execute('INSERT INTO comments(post_key,comment_id,level,fetch_order,body) VALUES(?,?,?,?,?)',
                (posts[row['post_key']],*[row[k] for k in ('comment_id','level','fetch_order','body')])).lastrowid
        for row in original_comments:
            if row['parent_key'] is not None:c.execute('UPDATE comments SET parent_key=? WHERE comment_key=?',(comments[row['parent_key']],comments[row['comment_key']]))
        for row in db.all('SELECT * FROM searches WHERE import_id=?',(old,)):
            sk=c.execute('INSERT INTO searches(import_id,query_text,region,station_poi,source_order,quality) VALUES(?,?,?,?,?,?)',(new,*[row[k] for k in ('query_text','region','station_poi','source_order','quality')])).lastrowid
            manifest['searches'][str(sk)]=search_meta.get(str(row['search_key']),{})
            for hit in db.all('SELECT * FROM search_results WHERE search_key=?',(row['search_key'],)):
                c.execute('INSERT INTO search_results(search_key,position,raw_note_id,raw_url,raw_title,raw_summary,quality) VALUES(?,?,?,?,?,?,?)',(sk,*[hit[k] for k in ('position','raw_note_id','raw_url','raw_title','raw_summary','quality')]))
        c.execute('UPDATE imports SET source_manifest=? WHERE import_id=?',(encode(manifest),new))
        for record in db.all('SELECT record_id,post_key FROM records WHERE profile_id=?',(profile_id,)):
            c.execute('UPDATE records SET post_key=?,version=version+1 WHERE record_id=?',(posts[record['post_key']],record['record_id']))
        c.execute('UPDATE profiles SET import_id=?,version=version+1 WHERE profile_id=?',(new,profile_id))
        result['to_import_id']=new
    return result
