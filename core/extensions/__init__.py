"""工单台扩展目录。每个扩展一个子目录,默认一个都不加载。

打开方法:在配置文件(core/desk_config.json,或 TICKET_DESK_CONFIG 指向的那份)的
「启用扩展」里写子目录名,例如 "启用扩展": ["deploy_record"]。
加载与钩子约定见 tools/tickets/extension_loader.py。

现有扩展:
  deploy_record  —— deploy-record 子命令、同名服务端方法与 /api/action 的 op:
                    部署脚本上服后自动建一张「上服记录」单(免判免复检,部署头写进当前值面)。
  server_deploy  —— 把工单台自己装到一台 Linux 服务器上的脚本(install/update/pack/backup),
                    没有 Python 钩子;账户名、服务名、安装目录都是变量,默认 ticket-desk。
"""
