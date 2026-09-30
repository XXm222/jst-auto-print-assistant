# 聚水潭安全打单助手

聚水潭订单打单的桌面客户端与后台协调服务。当前客户端版本为 **V0.5.25**，API schema 为 **5**。此仓库包含 2026-09-30 的强制跳过问题单更新。

## 本次更新

操作员可以通过“强制跳过异常单并继续”处理任意暂停类型的问题单。客户端停止当前工作线程后，将内部订单号、出库单号与原因提交给后台；后台持久保存永久排除记录，后续领取任务时跳过同一订单对。该操作不受旧异常类型白名单、已有租约或本机排除数量上限限制。

跳过必须有明确的订单身份，并成功写入后台才能继续。已发生的取号、打印或其他业务动作不会撤销，也不会被记为打印完成。后台接口为 `POST /jst-print-api/v1/order/force-skip`。

## 目录

- `jst_auto_print_app.py`：桌面界面、浏览器连接、队列执行与本地事件记录。
- `jst_print_shadow_plan.py`：只读订单规划与精确回读。
- `server_batch_v056/`：鉴权 API、SQLite 租约及永久跳过记录、systemd 部署模板。
- `tests/`：离线回归测试和单独执行的现场诊断工具。
- `build_jst_auto_print_win10_x64.bat`、`jst_auto_print_installer.iss`：Windows x64 构建及 Setup 定义。
- `build_jst_auto_print_macos.sh`：macOS 构建脚本。

## 配置和运行

仓库不包含生产凭据、真实订单日志、数据库或安装包。

1. 将 `jst_operator_config.example.json` 复制为 `jst_operator_config.json`。
2. 填入自己的 HTTPS 后台地址和 32 至 128 字符的 API token（只使用英文字母、数字、下划线及连字符）。客户端和后台使用同一 token。
3. 在 Windows x64 使用 CPython 3.10 64 位及 `py.exe`，然后运行 `Win10_21H1_源码直接启动.bat`。依赖由带 SHA256 的版本锁文件安装。
4. 离线自检可运行 `Win10_离线自检.bat`，或 `python jst_auto_print_app.py --self-test --self-test-output self-test.json`。

正式连接前需部署匹配后台，并配置本机 Chrome/Edge 和打印组件。仓库包含现有仓库、店铺及承运商规则；用于其他环境时先确认这些规则。后台调用的 ERP bridge 及其凭据是独立部署依赖，不包含在本项目中。部署基础说明见 `server_batch_v056/DEPLOYMENT_V0.5.21.md`；本次接口和迁移补充见 `docs/force-skip-20260930.md`。

“不打印试运行”仍可能执行改快递及获取电子面单号，应只用于已批准的验收订单。

## 测试与打包

```sh
python -m unittest discover -s tests -p 'test_*.py'
```

2026-09-30 更新已通过 361 项 macOS 回归测试；Windows 回归为 361 项、其中 3 项跳过。Windows x64 Setup 的安装及安装后自检通过。这些结果不替代真实订单和实体打印验收。

Windows 构建：先填写本机配置，再运行 `build_jst_auto_print_win10_x64.bat`。Setup 由 Inno Setup 编译 `jst_auto_print_installer.iss`。构建会将本机配置放入交付包，因此带生产 token 的产物只应交付给已授权电脑。
