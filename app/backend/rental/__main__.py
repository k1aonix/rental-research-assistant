import argparse
import json
from pathlib import Path

from .common import DomainError
from .config import Settings
from .db import Database
from .source import SourceImporter


def read_json(path):
    return json.loads(Path(path).read_text(encoding='utf-8-sig'))


def main():
    parser = argparse.ArgumentParser(description='租房助手V1.37后端及离线准备命令')
    parser.add_argument('--database', help='SQLite数据库路径；不覆盖原始raw.json')
    commands = parser.add_subparsers(dest='command', required=True)
    run = commands.add_parser('run')
    run.add_argument('--host', default='127.0.0.1')
    run.add_argument('--port', type=int, default=18080)
    source = commands.add_parser('source')
    source.add_argument('operation', choices=('inspect', 'validate', 'import'))
    source.add_argument('--input', required=True)
    source.add_argument('--manifest')
    source.add_argument('--version')
    source.add_argument('--authors', action='append', default=[], help='补充collect-author-titles生成的真实作者结果；仅在内存合并，不改原始文件')
    mapping = commands.add_parser('map-prepare')
    mapping.add_argument('--output', required=True)
    station_table = commands.add_parser('station-table', help='离线转换或校验人工站点表，不访问地图')
    station_table.add_argument('operation', choices=('inspect', 'convert'))
    station_table.add_argument('--input', required=True)
    station_table.add_argument('--output')
    check = commands.add_parser('check')
    settings = Settings.from_env()
    if parser_args := parser.parse_args():
        args = parser_args
    if args.database:
        settings.database = str(Path(args.database).resolve())
    if args.command == 'run':
        import uvicorn
        from .api import create_app
        uvicorn.run(create_app(settings), host=args.host, port=args.port, workers=1, access_log=False)
    elif args.command == 'source':
        data = read_json(args.input)
        for filename in args.authors:
            author_report = read_json(filename)
            author = author_report.get('data', author_report)
            author_id = author['author_id']
            data['author_history_runs'] = [item for item in data.get('author_history_runs', []) if item['author_id'] != author_id]
            data['author_history'] = [item for item in data.get('author_history', []) if item['author_id'] != author_id]
            status = author.get('quality', 'not_collected')
            data['author_history_runs'].append({'author_id': author_id, 'status': status,
                'fetched_at': author_report.get('fetched_at'), 'profile_text': author.get('profile_text'), 'error': author.get('error')})
            data['author_history'] += [{'author_id': author_id, **item} for item in author.get('history_titles', [])[:5]]
        inspected = SourceImporter.inspect(data)
        if args.operation == 'import':
            if not args.manifest or not args.version:
                parser.error('import需要--manifest和不可覆盖的--version')
            database = Database(settings.database)
            try:
                imported = SourceImporter(database).import_data(data, read_json(args.manifest), args.version)
                print(json.dumps({'import_id': imported, 'version': args.version, 'inspection': inspected}, ensure_ascii=False, indent=2))
            finally:
                database.close()
        else:
            print(json.dumps(inspected, ensure_ascii=False, indent=2))
            if not inspected['valid']:
                return 1
    elif args.command == 'station-table':
        from .station_table import StationTableRepository, convert_csv
        if args.operation == 'convert':
            if not args.output:parser.error('convert需要--output')
            if Path(args.output).exists():raise DomainError('STATION_TABLE_EXISTS', '输出文件已存在，请使用新版本文件名，保留原表及快照绑定。')
            table=convert_csv(args.input,args.output,settings.fixed_config())
        else:
            repository=StationTableRepository(args.input,settings.fixed_config())
            if repository.error:raise repository.error
            table=repository.table
        print(json.dumps({'version':table['version'],'table_hash':table['table_hash'],'station_count':len(table['stations']),
                          'coverage_limit_sec':table['coverage_limit_sec'],'network_calls':0},ensure_ascii=False))
    elif args.command == 'map-prepare':
        from .providers import MapGateway
        result = MapGateway(settings)._client().station_catalog()
        Path(args.output).write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
        print(json.dumps({'quality': result['quality'], 'coverage': result['coverage']}, ensure_ascii=False, indent=2))
        return 0 if result['coverage']['complete'] else 1
    else:
        from .providers import ModelGateway, MapGateway
        database = Database(settings.database)
        print(json.dumps({'database_schema': database.one('PRAGMA user_version'),
                          'source_imported': bool(database.one('SELECT import_id FROM imports LIMIT 1')),
                          'model_configured': ModelGateway(settings).ready(), 'map_ready': MapGateway(settings).ready(),
                          'station_selection': MapGateway(settings).capabilities()['station_discovery'],
                          'reference_date': settings.reference_date, 'policy': 'v1.37'}, ensure_ascii=False, indent=2))
        database.close()
    return 0


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except DomainError as error:
        print(json.dumps({'error': error.code, 'message': error.message, 'details': error.details}, ensure_ascii=False))
        raise SystemExit(1)
