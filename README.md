# Rental Research Assistant · 租房助手

租房助手将房源来源、调查证据、候选比较、看房、沟通和合同检查组织在同一工作流中。正式版采用 Starlette、Uvicorn、SQLite 和原生浏览器前端。独立演示用内置虚构房源呈现调查流程，真实沟通需要访问者自己的 OpenRouter Key。

## 本地启动

安装 Python 3.10 或更新版本。以下命令从仓库根目录执行。在 Windows 上可用 `.venv\Scripts\python.exe` 代替 `.venv/bin/python`，并把相对路径分隔符按 shell 调整。

```sh
python -m venv .venv
.venv/bin/python -m pip install -r app/backend/requirements.txt
cd app/backend
../../.venv/bin/python -m rental run --host 127.0.0.1 --port 18080
```

浏览器打开 http://127.0.0.1:18080/。初次启动会初始化本地数据库。使用系统环境变量配置服务，变量名见 `.env.example`。该文件是参考清单。需要真实模型或地图请求时分别设置 `MODEL_API_KEY` 和 `AMAP_WEB_KEY`。默认不发送 GA4 事件。

## 私有数据

参考 [数据接口](docs/data.md)，准备有权使用的来源 JSON、配套 manifest 和站点表。没有来源数据时只能验证基本启动，完整调查需要来源和外部服务。私有数据和凭证请存放在跟踪范围之外。

## 独立演示与测试

安装后在 `app/demo/latest` 运行 `python build_single.py`，打开生成的 `dist/index.html`。无 Key 可体验内置模拟流程与沟通样例。真实沟通使用访问者自己的 Key。

在 `app/backend` 运行 `python -m unittest discover -s tests -t . -p "test_*.py"`。使用临时数据库完成来源导入测试。真实服务验收需要独立测试配置。

## 许可

仓库候选的许可证及第三方许可声明待确认。
