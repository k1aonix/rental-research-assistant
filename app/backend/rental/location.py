"""Verified map candidates retained with the failed task; never accept client coordinates."""
import math
from .common import decode


def valid_location(value):
    try:
        x,y=map(float,value.split(','))
        return math.isfinite(x+y) and 113<=x<=115 and 22<=y<=23.5
    except (AttributeError,TypeError,ValueError):return False


def candidate(item):
    if not isinstance(item,dict) or not all(isinstance(item.get(k),str) and item[k] for k in ('id','name','location')):return None
    if not valid_location(item['location']):return None
    if item.get('cityname')!='深圳市' and not str(item.get('adcode','')).startswith('4403'):return None
    return {key:item.get(key) for key in ('id','name','location','address','adname','cityname','parent')}


def options(db,task,progress):
    if not task.get('record_id'):return []
    record=db.one('SELECT * FROM records WHERE record_id=?',(task['record_id'],))
    body=db.one("SELECT result FROM steps WHERE record_id=? AND step_name='body' AND state='succeeded'",(task['record_id'],))
    community=record['community_supplement'] or (decode(body['result']).get('community_name') if body else None)
    rows=((progress.get('last_error') or {}).get('details') or {}).get('candidates',[])
    saved=[value for row in rows if (value:=candidate(row))]
    if saved:return saved
    # Older failures did not expose coordinates. Read the actual saved POI response instead.
    for step in db.all("SELECT s.result,s.input_refs FROM steps s JOIN records r ON r.record_id=s.record_id WHERE r.profile_id=? AND s.step_name='map_http' AND s.state='succeeded'",(task['profile_id'],)):
        refs=decode(step['input_refs'],[])
        if any(r.get('endpoint')=='place/text' and r.get('params',{}).get('keywords')==community for r in refs):
            rows=[*rows,*decode(step['result'],{}).get('pois',[])]
    result={}
    for row in rows:
        value=candidate(row)
        if value:result[value['id']]=value
    return list(result.values())
