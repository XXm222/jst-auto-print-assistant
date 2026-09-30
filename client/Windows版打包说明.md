# 聚水潭安全打单助手 V0.5.25：Windows 构建与交付

本次已生成 V0.5.25 Windows x64 便携包并完成离线自检和清单校验；目标电脑和实体纸张验收仍需现场完成。历史 V0.5.24 成品不能当成 V0.5.25 发出。

## 构建

在仍处于微软安全支持期内的 Windows x64 电脑使用 CPython 3.10 AMD64。运行 `build_jst_auto_print_win10_x64.bat`。构建保持完整 SHA256 依赖锁、`--require-hashes --only-binary=:all:`、PyInstaller onedir 模式和每步失败检查，不使用退役的快速构建入口。

客户端使用 websocket-client 1.9.0 的原生 CDP/fetch 链路，不打包 Playwright driver。客户端和服务端均要求 API schema 5、planner schema 5；本次服务端无修改，最低客户端仍由后台 ping 返回。

构建脚本核验版本 0.5.25、配置、依赖、PE AMD64 架构、暂存 EXE 离线自检、ZIP 内容和 SHA256SUMS.txt。任一步失败不能发布产物。完整 ZIP（主 exe 约 2.1MB，运行库随目录一起提供）应为：

`dist_win10_x64/JSTAutoPrint_Win10_21H1_V0.5.25_SKUFix_20260914.zip`

安装器 `jst_auto_print_installer.iss` 的版本及 VersionInfoVersion 同步为 0.5.25，读取本次构建生成的 staging 目录。只能在构建门禁通过后生成安装包，不能拿旧目录替代。

## 操作员使用

1. 关闭旧助手，保留 `%USERPROFILE%/.jst-auto-print` 的历史任务与 SKU 数据。
2. 完整解压新 ZIP 到独立目录；不要只复制 EXE。
3. 运行包内 `Win10_离线自检.bat`。诊断输出在用户数据目录，不修改受清单保护的安装目录。
4. 后台连接异常时双击 `Win10_后台网络诊断.bat`。脚本会检查配置、系统代理、DNS、TCP 443、HTTPS/TLS 和三次认证 `/ping`，弹窗显示结论；脱敏日志保存在 `%USERPROFILE%\.jst-auto-print\diagnostics`。
5. 启动助手，确认窗口版本为 0.5.25。程序启动专用 Chrome/Edge，使用持久化独立 Profile；首次登录聚水潭一次，此后复用登录，不要求每次打开 Remote Debugging 设置。
6. 选择与实体纸张相符的面单类型，确认只有一个打单拣货页、本地打印组件在线。
7. “试运行不打印”仍会真实改快递和取号；只有批准的验收订单可以用于现场测试。正式运行前完成一单实体纸张、条码、运单及 SKU 核对。

## 数据与安全边界

按 `o_id + io_id` 精确确认任务、租约、仓库和动作结果；`waybill_fingerprint` 防止只比较运单后四位。示例身份 `9000001/19000001` 仅用于说明，不能作为真实验收单。

RUNNING 任务只读恢复，不自动重放。Confirmed 已提交任务以 UNCERTAIN_ACTION 转人工核对；没有 PRINT_OK 的订单不误记 SKU。订单隐私、停发、手工修改及拆单校验仍保留，不调用预发货/发货接口。

SHA256SUMS.txt 发现文件损坏，但不能证明发布者身份。没有 Authenticode 签名时不能称为已签名。后台配置含访问凭据，交付包仅供授权人员使用。

## 源码备用入口

`Win10_21H1_源码直接启动.bat` 会验证依赖并以不打印模式启动。原部署配置需保留；不要清空数据库。具体回归结果及未覆盖项见本次系统稳定性检查报告。
