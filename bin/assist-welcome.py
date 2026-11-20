"""Local first-launch consent; only the four public device-card fields leave this UI."""
import json
import os
from pathlib import Path
import subprocess
import sys


class Dialog:
    def __init__(self):
        self.root=None
        try:
            import tkinter as tk
            from tkinter import messagebox,simpledialog
            self.root=tk.Tk()
            self.root.withdraw()
            self.message=messagebox
            self.input=simpledialog
        except (ImportError,RuntimeError):
            pass
        except Exception as exc:
            if type(exc).__name__!='TclError':raise
        self.terminal=bool(sys.stdin and sys.stdin.isatty())
        if self.root is None and not self.terminal and os.name!='nt' and sys.platform!='darwin':
            raise ValueError('LOCAL_CONSENT_WINDOW_REQUIRED')

    def yes(self,prompt):
        if self.root:
            return self.message.askyesno('AS-BOT Hermes 插件',prompt,default='no',parent=self.root)
        if self.terminal:
            return input(prompt+' [y/N] ').strip().lower()=='y'
        if os.name=='nt':
            import ctypes
            return ctypes.windll.user32.MessageBoxW(None,prompt,'AS-BOT Hermes',0x124)==6
        script='on run a\nset d to display dialog (item 1 of a) buttons {"取消","同意"} default button "取消"\nreturn button returned of d\nend run'
        result=subprocess.run(['/usr/bin/osascript','-e',script,prompt],capture_output=True,text=True)
        return result.returncode==0 and result.stdout.strip()=='同意'

    def choose(self,labels,prompt):
        options='\n'.join(f'{n}. {label}' for n,label in enumerate(labels,1))
        if self.root:
            result=self.input.askinteger('AS-BOT Hermes 插件',prompt+'\n\n'+options,
                                         parent=self.root,minvalue=1,maxvalue=len(labels))
        elif self.terminal:
            answer=input(prompt+'\n'+options+'\n输入序号：').strip()
            result=int(answer) if answer.isdecimal() else None
        else:
            result=next((n for n,label in enumerate(labels,1) if self.yes(prompt+'\n'+label+'？')),None)
        if result is None or not 1<=result<=len(labels):
            raise ValueError('LOCAL_SELECTION_CANCELLED')
        return result-1

    def show_card(self,value,path):
        text=json.dumps(value,ensure_ascii=False)
        message='把下面的设备卡交给 AS-BOT 管理员。卡片也保存在：\n'+str(path)+'\n\n'+text
        if self.root:
            # A selectable field works even when the platform clears the
            # clipboard as the application exits.
            self.input.askstring('设备卡已准备好',message+'\n\n可从下方选中复制。',initialvalue=text,parent=self.root)
        elif self.terminal:
            print(message,file=sys.stderr)
        elif self.yes(message+'\n\n复制这张设备卡到剪贴板？'):
            if os.name=='nt':
                subprocess.run(['clip.exe'],input=text.encode('utf-16le'),check=True,timeout=5)
            else:
                subprocess.run(['/usr/bin/pbcopy'],input=text.encode(),check=True,timeout=5)

    def close(self):
        if self.root:self.root.destroy()

    def notice(self,text):
        if self.root:
            self.message.showinfo('AS-BOT Hermes 插件',text,parent=self.root)
        elif self.terminal:
            print(text,file=sys.stderr)
        elif os.name=='nt':
            import ctypes
            ctypes.windll.user32.MessageBoxW(None,text,'AS-BOT Hermes',0x40)
        else:
            script='on run a\ndisplay dialog (item 1 of a) buttons {"完成"} default button "完成"\nend run'
            subprocess.run(['/usr/bin/osascript','-e',script,text],capture_output=True)


def verify_identity(client,assist,path,dialog):
    with client.state_lock(path):
        current=client.load_state(path)
        transport=client.ConnectTransport()
        assist.refresh(client,path,current,transport)
        if (current['assisted'].get('withdrawn') or current.get('identityVerified')
                or current['remoteState'] not in ('awaiting_owner','awaiting_identity')):
            return assist.status_view(current)
        client.refresh_proof(path,current,transport)
        try:
            client.open_identity_browser(path,current,transport)
            return {**assist.status_view(current),'localIdentity':'BROWSER_OPENED'}
        except client.ClientError as exc:
            if exc.code not in ('CONNECT_HTTP_400','CONNECT_HTTP_404','CONNECT_HTTP_405','OAUTH_HANDOFF_PENDING',
                                'BROWSER_OPEN_FAILED_USE_DISCORD_COMMAND'):raise
        # A headless WSL environment or a site without OAuth can still use the
        # original Discord proof. Only the private local file contains the code.
        help_file=path/'local-identity.html'
        command='/接入确认 '+current['pairingCode']
        client.installer.atomic(help_file,('<!doctype html><meta charset="utf-8"><meta name="referrer" content="no-referrer">'
            '<title>核验我的 Discord</title><p>请用自己的 Discord 账号，在 AS-BOT 的 Discord 私下执行此命令。'
            '请勿发给管理员或转发这个文件。</p><pre>'+command+'</pre>').encode())
        current['localIdentityHelpAt']=client.time.time()
        client.save_state(path,current)
        if not client.webbrowser.open(help_file.as_uri()):
            dialog.notice('请在本机浏览器打开以下私有文件，按说明验证自己的 Discord。请勿转发。\n'+str(help_file))
        return {**assist.status_view(current),'localIdentity':'PRIVATE_INSTRUCTIONS_READY'}


def welcome(args,client):
    from contextlib import closing
    assist=client.helper('assisted')
    if any(os.environ.get(key) for key in ('SSH_CONNECTION','SSH_CLIENT','SSH_TTY')):
        raise ValueError('RECIPIENT_LOCAL_WINDOW_REQUIRED')
    path=Path(args.state_dir).expanduser().absolute()
    with closing(Dialog()) as dialog:
        current=None
        if path.exists() or path.is_symlink():
            path=client.private_directory(path)
            current=client.load_state(path)
        if current and 'assisted' not in current:
            raise ValueError('EXISTING_INSTALLATION_REQUIRES_OWNING_UPGRADE_PATH')
        if current:
            if current['assisted'].get('withdrawn'):
                with client.state_lock(path):
                    return assist.withdraw(client,path,client.load_state(path),client.ConnectTransport())
            option=dialog.choose(['查看设备卡并恢复原申请','在本机验证我的 Discord','在本机撤回协助授权'],'管理这台设备')
            if option==2:
                if not dialog.yes('停止这台设备的安装与后续维护？已安装的插件可继续按自己的账号权限使用。'):
                    return {'stage':'CANCELLED','qualified':False}
                with client.state_lock(path):
                    return assist.withdraw(client,path,client.load_state(path),client.ConnectTransport())
            args.profile=current['profile']
            args.allow_maintenance=current['assisted']['maintenance']
            args.allow_idle_restart=current['assisted']['restart']
            if option==1:
                return verify_identity(client,assist,path,dialog)
        else:
            if not dialog.yes('允许 AS-BOT 在你已有的 Hermes 中安装截图和经济日历插件吗？\n'
                              '你会选择本机已批准的微信私聊。确认后设备将向 AS-BOT 发起 HTTPS 联系，'
                              '等待管理员接入，首次安装最多等待 24 小时。'):
                return {'stage':'CANCELLED','qualified':False}
            args.allow_maintenance=dialog.yes('允许安装完成后继续维护这个插件吗？\n设备登录期间会定期联系 AS-BOT，'
                                              '只接受此配置的签名插件更新、诊断和修复。可以随时在此撤回。')
            args.allow_idle_restart=dialog.yes('若插件绑定需要，允许在已核实空闲时重启现有的 Hermes Gateway 吗？'
                                               '\n这也适用于本次更新失败、恢复旧版后的重启。')
        path=client.private_directory(path)
        with client.state_lock(path):
            transport=client.ConnectTransport()
            current=client.load_state(path)
            if current and current.get('created'):
                # A newer certified installer can display or verify an existing
                # card without acquiring its profile or broadening its consent.
                assist.refresh(client,path,current,transport)
                if current['assisted'].get('withdrawn'):
                    return assist.status_view(current)
                prefs=current['assisted']
                if (prefs.get('householdTimer') and not prefs.get('scheduled') and current['remoteState'] not in assist.TERMINAL
                        and (prefs['maintenance'] or not prefs.get('last') and client.time.time()-prefs['createdAt']<assist.DAY)):
                    root=path/'assist-bootstrap' if (path/'assist-bootstrap').is_dir() else client.ROOT
                    package=client.installer.verify_payload(root)
                    if package['sha']!=prefs['sourceSha']:raise ValueError('ASSIST_BOOTSTRAP_CHANGED')
                    client.helper('assist-schedule').enable(client,path,package)
                    prefs['scheduled']=True
                    client.save_state(path,current)
                result=assist.card(current)
            else:
                result=assist.setup(client,path,args,transport,choose=dialog.choose)
                current=client.load_state(path)
        dialog.show_card(result,path/'device-card.json')
        if (current['remoteState'] in ('awaiting_owner','awaiting_identity') and not current.get('identityVerified')
                and dialog.yes('会员需要本人验证时，可以在本机的浏览器登录自己的 Discord。现在验证吗？'
                               '\n也可以稍后重新打开安装器，选择“在本机验证我的 Discord”。')):
            verify_identity(client,assist,path,dialog)
        return result
