"""Versioned manual station references. Selection performs no network calls."""
import copy,json,csv,math,hashlib
from decimal import Decimal,InvalidOperation
from pathlib import Path
from .common import DomainError,digest

STRATEGY='manual_station_times_v1'

class StationTableRepository:
    def __init__(self,path,target):
        self.table=None;self.error=None
        try:
            if not path or not Path(path).is_file():raise DomainError('STATION_TABLE_REQUIRED','人工站点时间表尚未配置。',503)
            self.table=self.validate(json.loads(Path(path).read_text(encoding='utf-8-sig')),target)
        except DomainError as error:self.error=error
        except (OSError,ValueError,TypeError):self.error=DomainError('STATION_TABLE_INVALID','人工站点时间表无法读取或JSON格式无效。',503)

    @staticmethod
    def invalid(field,row=None):
        raise DomainError('STATION_TABLE_INVALID','人工站点表字段无效，未跳过或填0。',503,field=field,row=row)

    @classmethod
    def validate(cls,table,target):
        if not isinstance(table,dict):cls.invalid('table')
        table=copy.deepcopy(table)
        for key in ('version','time_basis','transport_mode','prepared_at','source_note'):
            if not isinstance(table.get(key),str) or not table[key].strip():cls.invalid(key)
        header=table.get('target_snapshot',{})
        if not isinstance(header,dict):cls.invalid('target_snapshot')
        if table.get('target_id')!=target.get('target_poi') or header.get('target_poi')!=target.get('target_poi'):
            raise DomainError('STATION_TABLE_TARGET_MISMATCH','人工时间表与画像目标不一致。',503)
        def location(value):
            try:
                x,y=map(float,value.split(','))
                if not math.isfinite(x+y) or not 73<=x<=135.5 or not 2.5<=y<=54:raise ValueError()
                return x,y
            except (AttributeError,ValueError,TypeError):cls.invalid('location')
        if location(header.get('location'))!=location(target.get('location')):
            raise DomainError('STATION_TABLE_TARGET_MISMATCH','人工时间表目标坐标不一致。',503)
        if type(table.get('coverage_limit_sec')) is not int or table['coverage_limit_sec']<=0:cls.invalid('coverage_limit_sec')
        if not isinstance(table.get('stations'),list) or not table['stations']:cls.invalid('stations')
        ids=set();pois=set();coordinates=set()
        for index,row in enumerate(table['stations'],1):
            if not isinstance(row,dict):cls.invalid('station',index)
            for key in ('station_id','station_name'):
                if not isinstance(row.get(key),str) or not row[key].strip():cls.invalid(key,index)
            if row['station_id'] in ids:cls.invalid('duplicate_station_id',index)
            ids.add(row['station_id'])
            if type(row.get('enabled')) is not bool or type(row.get('display_order')) is not int or row['display_order']<0:cls.invalid('enabled/display_order',index)
            if not isinstance(row.get('aliases'),list) or any(not isinstance(s,str) or not s for s in row['aliases']):cls.invalid('aliases',index)
            if row.get('location'):
                point=location(row['location'])
                if row.get('coordinate_system')!='GCJ-02':cls.invalid('coordinate_system',index)
                if point in coordinates:cls.invalid('duplicate_physical_location',index)
                coordinates.add(point)
            if row.get('poi_id'):
                if not isinstance(row['poi_id'],str) or row['poi_id'] in pois:cls.invalid('duplicate_or_invalid_poi',index)
                pois.add(row['poi_id'])
            if not row.get('poi_id') and not row.get('location'):cls.invalid('poi_id/location',index)
            if 'reference_duration_sec' in row:
                seconds=row['reference_duration_sec']
                if type(seconds) is not int or seconds<0:cls.invalid('reference_duration_sec',index)
            else:
                try:
                    if isinstance(row.get('reference_duration_min'),bool):raise ValueError()
                    seconds=Decimal(str(row['reference_duration_min']))*60
                    if not seconds.is_finite() or seconds<0 or seconds!=seconds.to_integral_value():raise ValueError()
                    seconds=int(seconds)
                except (KeyError,ValueError,InvalidOperation):cls.invalid('reference_duration_min',index)
                row['reference_duration_sec']=seconds
            if 'reference_duration_min' in row:
                try:
                    if Decimal(str(row['reference_duration_min']))*60!=seconds:cls.invalid('conflicting_duration_units',index)
                except InvalidOperation:cls.invalid('reference_duration_min',index)
            if seconds==0 and row.get('zero_confirmed') is not True:cls.invalid('zero_confirmed',index)
            if seconds>table['coverage_limit_sec']:cls.invalid('duration_exceeds_table_coverage',index)
            refs=row.get('snapshot_refs',[])
            if not isinstance(refs,list):cls.invalid('snapshot_refs',index)
            for ref in refs:
                if not isinstance(ref,dict) or ref.get('verified') is not True or not all(isinstance(ref.get(k),str) and ref[k] for k in ('source_version','search_id','keyword')):cls.invalid('snapshot_refs',index)
        table['table_hash']=digest({k:v for k,v in table.items() if k!='table_hash'})
        return table

    def freeze(self):
        if self.error:return {'strategy':STRATEGY,'error':{'code':self.error.code,'message':self.error.message,'details':self.error.details}}
        return {'strategy':STRATEGY,'table_version':self.table['version'],'table_hash':self.table['table_hash'],'table':copy.deepcopy(self.table)}

    @classmethod
    def select(cls,target,limit,frozen):
        if frozen.get('strategy')!=STRATEGY:
            raise DomainError('LEGACY_DISCOVERY_POLICY','旧任务未冻结人工站点表，请保留历史并新建搜索。',409)
        if frozen.get('error'):
            error=frozen['error'];raise DomainError(error['code'],error['message'],503,**error.get('details',{}))
        if type(limit) is not int or limit<0:cls.invalid('commute_limit_sec')
        table=cls.validate(frozen.get('table'),target)
        if table['table_hash']!=frozen.get('table_hash') or table['version']!=frozen.get('table_version'):
            raise DomainError('STATION_TABLE_SNAPSHOT_INVALID','冻结人工表版本或内容hash不一致。',503)
        selected=[];excluded=[]
        for row in table['stations']:
            if not row['enabled'] or row['reference_duration_sec']>limit:
                excluded.append({'station_id':row['station_id'],'reason':'disabled' if not row['enabled'] else 'over_reference_limit'});continue
            selected.append({**row,'canonical_station_id':row['station_id'],'poi_ids':[row['poi_id']] if row.get('poi_id') else [],
                             'name':row['station_name'],'sort_key':[row['reference_duration_sec'],row['display_order'],row['station_id']]})
        selected.sort(key=lambda x:tuple(x['sort_key']))
        return {'strategy':STRATEGY,'quality':'success','selection_state':'ready','table_version':table['version'],'table_hash':table['table_hash'],
                'time_basis':table['time_basis'],'transport_mode':table['transport_mode'],'source_note':table['source_note'],
                'coverage_limit_sec':table['coverage_limit_sec'],'table_coverage_limited':limit>table['coverage_limit_sec'],
                'commute_limit_sec':limit,'stations':selected,'excluded':excluded,
                'outcome':'stations_selected' if selected else 'no_station_within_reference_limit'}


def convert_csv(directory,output,target):
    directory=Path(directory);path=directory/'60分钟以内地铁站.csv'
    with path.open(encoding='utf-8-sig',newline='') as source:
        rows=list(csv.DictReader(source))
    stations=[]
    for i,row in enumerate(rows,1):
        if row['终点POI']!=target['target_poi'] or row['终点坐标_GCJ02']!=target['location']:
            raise DomainError('STATION_TABLE_TARGET_MISMATCH','CSV目标不一致。',422,row=i+1)
        try:seconds=int(row['参考秒数'])
        except (ValueError,KeyError):StationTableRepository.invalid('参考秒数',i+1)
        stations.append({'station_id':row['POI_ID'],'station_name':row['站名'],'address':row['地址或POI'],'poi_id':row['POI_ID'],
            'location':row['坐标_GCJ02'],'coordinate_system':'GCJ-02','reference_duration_sec':seconds,
            'source_display_min':row['参考分钟数'],'display_order':i,'enabled':True,'aliases':[row['站名']],
            'source_note':row['时间口径/来源'],'source_url':row['来源URL'],'queried_at':row['查询时间'],
            'time_basis':{'reference_date':row['参考日期'],'departure_time':row['出发时间']},'snapshot_refs':[]})
    table={'version':'manual-20261121-'+hashlib.sha256(path.read_bytes()).hexdigest()[:12],'target_id':target['target_poi'],
           'target_snapshot':{k:target[k] for k in ('target_poi','target_name','location')},'coverage_limit_sec':3600,
           'time_basis':'2026-11-21 08:30 Asia/Shanghai；人工提供的高德历史预计值，非实测、非当前日期实时结果',
           'transport_mode':'地铁及步行（附近站含直接步行）','prepared_at':max(r['查询时间'] for r in rows),
           'source_note':'用户提供CSV；判定使用原始参考秒数，参考分钟仅显示；只覆盖表内60分钟以内52站。',
           'source_sha256':hashlib.sha256(path.read_bytes()).hexdigest(),'stations':stations}
    normalized=StationTableRepository.validate(table,target)
    Path(output).write_text(json.dumps(normalized,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    return normalized
