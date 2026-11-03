# News Catch

[English](README.md) | [简体中文](README.zh-CN.md)

运行在本地浏览器中的新闻采集、筛选、AI 分析、人工审核和 Webhook 推送工作台。Python 后端、原生 HTML/CSS/JavaScript 前端、SQLite 本地存储。此源码版基于 2026-11-03 的工作台生成。

目前界面和生成的摘要使用中文。

## 干净初始状态

不预置新闻源、关键词规则、目标区域、模型地址/名称/密钥、Webhook 或字段映射；不携带原用户新闻、原文、推送记录、数据库或内部任务文档。首次使用需自行配置。

保留类别、地区、发布阶段等文章字段及选项，以及现有车载语音/AI 市场分析逻辑；此版未扩展为适用于任意行业的通用分类引擎。

## 功能

- RSS / 网页来源管理，OPML 批量导入、来源连通测试及健康状态。
- 网页正文抓取、人工原文补充，关键词布尔表达式筛选、去重、区域/类别判定。
- 可配置 OpenAI 兼容模型 API，单条与批量后台分析、长正文分段、证据校验及中文摘要。
- 人工编辑审核、待推送队列、退回重审、软删除与恢复。
- Webhook 字段新增/删除/映射，可编辑 JSON 预览、失败重试及推送历史。
- 网页快照变化跟踪、运行概览、定时任务、Markdown 周报。

## Windows 启动

1. 安装 **Python 3.11 或更新版本**，安装时启用 Python Launcher 或加入 `PATH`。
2. 下载并完整解压本仓库，双击 **start.cmd**。首次联网创建 `.venv` 并安装锁定依赖；后续使用本地环境启动。
3. 浏览器自动打开工作台。在「来源与规则」添加来源（也可导入 OPML）、新增命名规则并填写表达式，再保存。表达式支持 `AND`、`OR`、`NOT`、括号和双引号短语；规则为空时普通新闻不入池。
4. 在「模型与 Webhook」填写服务地址、模型名、密钥及自己的 Webhook；用「新增字段」配置接收字段映射。存在待推送新闻但未配置任何映射时，会阻止推送。
5. 点击「立即运行」，按需抓取正文、分析、审核、预览和推送。

首次安装需要能访问 Python 包源，采集和模型调用需要相应网络及接口权限。本源码版不附带 Python 或预设账号。若系统 Python 升级，请删除本项目 `.venv` 后重新启动以重建环境。

## 手动启动 / Linux / macOS

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python -B launch.py
```

Windows 对应使用 `.venv\Scripts\python.exe`。主要业务逻辑跨平台，当前分发仅在 Windows x64 / Python 3.11 环境验证；未实测 Linux 和 macOS。

启动器默认只监听 `127.0.0.1`，自动选择 4173–4192 中的空闲端口；可以传入 `--port 4216`、`--no-browser` 或 `--no-scheduler`。关闭终端后服务和定时任务停止，电脑休眠期间不会执行。时间设置按 Asia/Shanghai（GMT+8），默认 10:00，启动晚于计划时间可能补执行。

## 检查与测试

安装依赖后，Windows 使用虚拟环境检查及测试：

```powershell
.\.venv\Scripts\python.exe -B launch.py --check
.\.venv\Scripts\python.exe -B -m unittest discover -s tests -v
```

Linux / macOS 对应使用 `.venv/bin/python`。

`--check` 使用临时数据检查依赖和空默认配置，不访问模型/Webhook；测试使用隔离目录与模拟响应。可用 `--data-dir` 指定独立数据目录。

## 数据与共享

运行后 `workbench/data/config.json` 保存个人配置（包含密钥和 Webhook），`workbench/data/trenddesk.sqlite3` 保存新闻及历史；需妥善保管。停止服务后备份整个 `workbench/data`。

这是本地单机工作台，各电脑数据不会自动同步。

## 发布或分享自己的副本

将本文件夹作为仓库根目录。已提供 `.gitignore` 排除运行数据、数据库、密钥文件、虚拟环境和测试输出。初始源码包已去除私人配置；运行使用后再次发布时，仍应检查待提交文件。

## 项目结构

```text
News Catch/
  start.cmd / bootstrap.py  Windows 首次依赖安装与启动
  launch.py                包内路径、独立数据与本地端口
  workbench/               完整业务源码及锁定依赖
  tests/                   干净配置回归验证
  LICENSE                  MIT
```

## 当前边界

网页抽取无法保证访问需登录、限制自动抓取或仅通过前端脚本动态渲染的内容，可改用人工原文。AI 输出需要人工核验。

当前无多人账号权限与公网部署加固，适合本机运行；云端协作需要另行改造。Webhook 是否写入成功由接收方返回与字段类型决定。

## 许可证

项目代码采用 [MIT 许可证](LICENSE)；第三方依赖保留各自许可证，见 [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)。
