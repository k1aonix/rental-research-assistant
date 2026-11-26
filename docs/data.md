# 私有来源数据接口

来源快照为 JSON 对象。请保持稳定 ID，确保评论和搜索位置引用的帖子存在。

| 数组 | 用途与主要关联字段 |
| --- | --- |
| notes | 帖子，含 note_id、author_id、title、body、original_url 和采集状态 |
| comments | 评论，含 comment_id、note_id、parent_comment_id 和 body |
| search_runs | 搜索记录，含 search_id、round_id、keyword、status 和覆盖上下文 |
| search_results | 搜索位置，含 search_id、note_id、position 和 status |
| author_history | 作者历史记录，含 author_id、note_id、title |
| author_history_runs | 作者历史采集状态，含 author_id、status、fetched_at |

配套 manifest 见 app/backend/source-manifest.example.json。应填写实际覆盖、搜索映射及小区数据。站点表是独立输入，通过 STATION_TABLE_PATH 配置；可用 station-table inspect 检查。

从 app/backend 目录运行：

```sh
python -m rental source validate --input /path/to/raw.json
python -m rental --database /path/to/isolated.sqlite source import --input /path/to/raw.json --manifest /path/to/manifest.json --version unique-source-version
python -m rental station-table inspect --input /path/to/station-table.json
```

先用隔离库检验数量、引用、覆盖和缺失位置。每次导入对应一个冻结批次。仅替换 raw.json 不会更新库中的来源批次。
