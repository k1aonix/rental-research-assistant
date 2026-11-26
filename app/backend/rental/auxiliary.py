"""Collect available auxiliary evidence without confusing absence with verified emptiness."""
from .common import DomainError

POLICY='available_evidence_v1'
WORDS={'not_collected':'未采集','failed':'读取失败','partial':'仅部分取得','success':'已取得','empty_confirmed':'已确认真实为空'}
STATUSES={'success':'complete','partial':'partial','failed':'failed','not_collected':'not_collected','empty_confirmed':'empty_confirmed'}


def collect(engine,task,progress,post,community,stage,scope,preferences):
    evidence=[];missing=[];sources=[];warnings=[]
    def source(kind,ref,status,reason=None):
        row={'source_kind':kind,'source_ref':ref,'collection_status':STATUSES.get(status,status),'reason':reason,
             'impact':'仅影响证据完整性，不单独阻断；使用现有全部证据在S6判断。'}
        sources.append(row)
        if reason and status not in ('success','complete','empty_confirmed','skipped_optional'):missing.append(reason)
    def comments(item,kind):
        q=item['quality']['primary_comments'];rows=engine.svc.source.comments(item['post_key'])
        if rows and q=='empty_confirmed':q='partial';warnings.append('EMPTY_MARKER_CONFLICTS_WITH_CONTENT')
        if not rows and q=='success':q='not_collected'
        source(kind,item['note_id'],q,None if q in ('success','empty_confirmed') else '评论资料'+WORDS.get(q,q)+'：'+item['note_id'])
        for row in rows:
            evidence.append(engine._evidence(task,item,row['body'],{'type':'comment','comment_id':row['comment_id']},stage,comment=row))
        replies=item['quality'].get('replies','not_collected')
        if (rows or q!='empty_confirmed') and replies not in ('success','empty_confirmed'):source(kind+'_replies',item['note_id'],replies,'二级回复完整性未确认：'+item['note_id'])
    try:
        if stage=='preference':
            source('preference',scope,'skipped_optional','未执行专项搜索；S6复用原帖、评论、作者及小区现有证据。')
        elif stage=='comments':
            engine._read(progress,'comments',note_id=post['note_id']);comments(post,'original_comments')
        elif stage=='author':
            engine._read(progress,'author',author_key=post['author_key']);a=engine.svc.source.author(post['author_key'])
            q=a['quality']['status'] if a else 'not_collected'
            if a and q=='empty_confirmed' and a['history_titles']:q='partial';warnings.append('EMPTY_MARKER_CONFLICTS_WITH_CONTENT')
            if a and q=='success' and not any((a.get('nickname'),a.get('profile_text'),a['history_titles'])):q='not_collected'
            source('author',a['author_id'] if a else None,q,None if q in ('success','empty_confirmed') else '作者资料'+WORDS.get(q,q)+'，身份及历史完整性未核实。')
            if a:
                text='\n'.join(t for t in (a.get('nickname'),a.get('profile_text')) if t)
                if text:evidence.append(engine._evidence(task,None,text,{'type':'author_profile','author_id':a['author_id']},stage,author=a))
                for i,title in enumerate(a['history_titles'][:5]):
                    if title.get('title'):evidence.append(engine._evidence(task,None,title['title'],{'type':'history_title','index':i,'note_id':title.get('note_id'),'url':title.get('original_url')},stage,author=a))
        else:
            engine._read(progress,'community',community=community,preference=None)
            posts=[p for p in engine.svc.source.auxiliary(post['import_id'],community) if p['post_key']!=post['post_key']]
            if not posts:
                searches=engine.db.all("SELECT quality FROM searches WHERE import_id=? AND region='community' AND instr(query_text,?)>0",(post['import_id'],community))
                status='empty_confirmed' if searches and all(s['quality']=='empty_confirmed' for s in searches) else 'failed' if searches and all(s['quality']=='failed' for s in searches) else 'not_collected'
                source('community',community,status,None if status=='empty_confirmed' else '没有可用小区调查帖；未强制补齐。')
            for item in posts:
                q=item['quality']['body'];body=item.get('body')
                if not body and q=='success':q='not_collected'
                source('community_post',item['note_id'],q,None if q=='success' else '小区帖正文'+WORDS.get(q,q)+'：'+item['note_id'])
                if body:
                    evidence.append(engine._evidence(task,item,(item.get('title') or '')+'\n'+body,{'type':'body','note_id':item['note_id']},stage))
                comments(item,'community_comments')
    except DomainError as error:
        if error.code not in ('SOURCE_NOT_FOUND','SOURCE_UNAVAILABLE','NECESSARY_SOURCE_FAILED'):raise
        source(stage,None,'failed',error.message);warnings.append(error.code)
    except (ValueError,TypeError,KeyError):
        source(stage,None,'failed','辅助来源结构不可读，保留此前取得的有效证据。');warnings.append('AUXILIARY_SOURCE_INVALID')
    statuses=[s['collection_status'] for s in sources]
    status=('skipped_optional' if stage=='preference' else 'partial' if evidence and any(s not in ('complete','empty_confirmed') for s in statuses)
            else 'failed' if not evidence and 'failed' in statuses else 'not_collected' if not evidence and 'not_collected' in statuses
            else 'partial' if 'partial' in statuses else 'empty_confirmed' if not evidence and statuses and all(s=='empty_confirmed' for s in statuses) else 'complete')
    return {'community':community,'preference':next((p['original_text'] for p in preferences if p['preference_id']==scope),None),
            'evidence':list({e['evidence_id']:e for e in evidence}.values()),'missing':list(dict.fromkeys(missing)),
            'collection_status':status,'sources':sources,'warnings':warnings,'auxiliary_policy':POLICY,
            'skip_model':stage=='preference' or (stage=='author' and any(s['collection_status'] in ('failed','not_collected') for s in sources))}


def annotate(result,payload):
    return {**result,'missing':list(dict.fromkeys(result.get('missing',[])+payload['missing'])),
            'collection_status':payload['collection_status'],'sources':payload['sources'],'warnings':payload['warnings'],
            'used_source_refs':[e['locator'] for e in payload['evidence']],'auxiliary_policy':POLICY}


def completeness(investigations):
    sources=[];warnings=[]
    for row in investigations:
        if row['stage']=='body':continue
        result=row['result'];sources.extend(result.get('sources',[]));warnings.extend(result.get('warnings',[]))
        if not result.get('auxiliary_policy'):
            sources.append({'source_kind':row['stage'],'source_ref':None,'collection_status':'partial','reason':'复用历史成功调查，旧结果未标注完整性。','impact':'不能由历史步骤成功推断资料齐全。'})
    gaps=[s for s in sources if s['collection_status'] not in ('complete','empty_confirmed')]
    return {'auxiliary_policy':POLICY,'sources':sources,'missing':gaps,'warnings':list(dict.fromkeys(warnings)),
            'interpretation':'报告完成表示现有资料分析结束，不等于资料齐全或每项偏好已证实符合。'}
