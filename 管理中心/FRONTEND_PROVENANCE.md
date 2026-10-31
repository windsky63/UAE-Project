# 参考界面与复用记录

用户指定参考为桌面“AI助手”中的legacy-system，实际定位为 `D:/Desktop/语言模型助手/legacy-system/web`。

直接复用：`src/main.js`（Vue/Vuetify注册和主题）、`src/style.css`（保存为legacy.css）、`ActionIcon.vue`、`HelpTip.vue`、`LogsPanel.vue`。LogsPanel保留分页、错误反馈和自动刷新，替换日志说明并添加工具筛选。

沿用布局：248px左侧导航、64px顶栏、白色表面、浅灰工作区、蓝色选中项、圆角表单及表格。业务导航和表单替换为同步、上传和微信三个本地工具，不复用旧服务的身份、模型配置或API。前端依赖从参考项目的本机安装复制，用于离线构建。
