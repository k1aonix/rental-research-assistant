"""Read-only local credential/route preflight. Never emit URLs containing keys."""
import json,urllib.request,urllib.error
from .config import Settings
from .providers import MapGateway

def main():
    settings=Settings.from_env()
    try:
        request=urllib.request.Request('https://openrouter.ai/api/v1/key',headers={'Authorization':'Bearer '+settings.model_key,'Accept':'application/json'})
        with urllib.request.urlopen(request,timeout=20) as response:
            data=json.load(response)['data']
        if data.get('is_management_key') or data.get('is_provisioning_key'):raise ValueError('inference key required')
        context=settings.planning_context()
        result=MapGateway(settings)._client().request('direction/transit/integrated',origin='114.211332,22.705924',destination=settings.target_location,city='深圳',cityd='深圳',strategy=0,extensions='all',date=context['reference_date'],time=context['departure_time'])
        print(json.dumps({'ok':True,'date':context['reference_date'],'calendar_policy':context.get('calendar_policy'),'departure_time':context['departure_time'],'model_authenticated':True,'map_status':result.get('status'),'route_count':len(result.get('route',{}).get('transits',[]))},ensure_ascii=True))
        return 0
    except Exception as error:
        print(json.dumps({'ok':False,'error_type':type(error).__name__,'code':getattr(error,'code',None)},ensure_ascii=True));return 1
    finally:settings.model_key='';settings.amap_key=''

if __name__=='__main__':raise SystemExit(main())
