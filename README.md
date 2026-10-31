# 阿联酋项目工具集

包含 FusionLive 同步工具、绿联云上传工具、管理中心及 codex-wechat。

## 本地配置

- Python 依赖见根目录及各子项目的 `requirements.txt`。
- 微信：复制 `codex-wechat/wechat.codex.example.json` 为 `wechat.codex.json`，填写自己的账号与会话。
- FusionLive：复制 `FusionLive同步工具/app/local_credentials.example.py` 为同目录的 `local_credentials.py`，通过环境变量 `FUSIONLIVE_USERNAME`、`FUSIONLIVE_PASSWORD` 配置凭据。
- 上传工具：复制 `绿联云上传工具/config.example.json` 为 `config.json`，填写实际源目录、台账和目标路径。
- 管理中心网络配置参考 `管理中心/network_config.example.json`。

账号密码、真实本地配置、数据库、会话记录、日志、业务文件、虚拟环境及发布压缩包均不纳入版本控制。克隆后需配置本机路径与账号。各组件的操作说明见对应目录。
