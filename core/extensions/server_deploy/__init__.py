"""扩展:把工单台自己装到一台 Linux 服务器上(systemd)的脚本。

  install.sh  首次安装:建服务账号、目录、自签证书、服务令牌、管理员账号,写 systemd 单元;
  update.sh   每次上服:三道前置闸(Pillow / pytest / tickets.js 语法)+ 替换 + 端口后置闸;
              后置闸通过后尝试建一张上服记录(服务端要启用扩展 deploy_record 才建得成);
  pack.sh     打上服包(tools/tickets、tools/browser、extensions、desk_config.json),把提交号写进包里;
  backup.sh   每日备份(ticket.py dump),只留最近 14 天;
  上服清单.md  设计者在上服前后要亲手做的几件事。

账户名、服务名、安装根、端口都是变量(环境变量),默认 ticket-desk / ticket-desk / /srv / 8443,
写法见各脚本开头。本扩展没有 Python 钩子:启用与否不改变命令行与服务端行为,
写进「启用扩展」只是为了让它的用例在上服闸里跟着跑。
"""
