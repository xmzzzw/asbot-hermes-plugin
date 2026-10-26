"""Zero-model commands on public Hermes admission/task/adapter APIs only.

The factory retains its existing adapter; it never modifies that adapter or
imports a Weixin SDK/protocol. Hermes still owns inbound deduplication and busy
handling. A recognized command always returns handled, including on failure.
"""
import asyncio
from contextvars import copy_context
from dataclasses import replace
import hashlib
from pathlib import Path
import re
import time

from .bridge import Bridge, BridgeError, Identity, bind_inbound, private_file, validate_png
from .deliveries import Deliveries

JOB_SECONDS = 600
SEND_SECONDS = 60
NOTICE_SECONDS = 15
HELP = ("截图：1 / BTC1 / 截图1；支持 0–9，单条最多 5 项。\n"
        "同模板新图可发 1 #A01、1 #A02；编号只区分消息文本。\n"
        "明确指令不调用模型；普通聊天保留原模型路径。\n"
        "截图状态：查看已记录任务。结果未知时不自动补发。\n"
        "Hermes 入站去重和忙时策略仍生效；不保证原样重复或忙时旁路。")


def parse(text):
    if not isinstance(text, str):
        return None
    value = text.strip()
    if len(value)>256:
        return ('invalid', None) if re.match(r'^(?:BTC|截图|/asbot|/截图)',value,re.I) else None
    value = re.sub(r'\s+(?:#|编号\s*[:：]?\s*)[A-Za-z0-9][A-Za-z0-9_-]{0,31}$','',value)
    if value in ('截图帮助','/asbot','/截图','/asbot help'):
        return 'help', None
    if value in ('截图状态','/asbot status','/截图 状态'):
        return 'status', None
    value = re.sub(r'^/(?:asbot|截图)\s+','',value,flags=re.I)
    token = r'(?:(?:BTC|截图)\s*)?([0-9])'
    if re.fullmatch(token+r'(?:[\s,，、]+'+token+r')*',value,re.I):
        return 'capture', tuple(re.findall(token,value,re.I))
    if value.isdigit() or re.match(r'^(?:BTC|截图)(?:\s*\d|\s*$)',value,re.I):
        return 'invalid', None
    return None


def identity(profile,source,message_id,session_key,platform):
    if (platform!='weixin' or not isinstance(source,dict) or
            (source.get('profile') or 'default')!=profile or source.get('chat_type')!='dm'
            or source.get('platform',platform)!=platform
            or source.get('is_bot') is True or source.get('profile_route_rejected') is True):
        raise BridgeError('PLATFORM_ROUTE_UNVERIFIED')
    values=(source.get('user_id'),source.get('chat_id'),message_id,session_key)
    if any(not isinstance(v,str) or not v or len(v)>512 or any(ord(c)<32 or ord(c)==127 for c in v) for v in values):
        raise BridgeError('ADMITTED_MESSAGE_REQUIRED')
    return Identity(profile,platform,*values[:3])


def handled(code, message):
    return {'action':'handled','reply':message+' ['+code+']'}


def template_text(shortcut, description):
    # Catalog text is plain display data, never a command, model prompt or path.
    label = ' '.join(description.split())[:400] if isinstance(description,str) else ''
    label = ''.join(c for c in label if ord(c)>=32 and ord(c)!=127)
    return str(shortcut)+'、'+(label or 'BTC'+str(shortcut))


class FastLane:
    def __init__(self,ctx,profile,config_path):
        self.ctx,self.profile,self.config_path=ctx,profile,config_path
        self.adapter=None;self.loop=None;self.closed=False;self.epoch=0
        self.tasks=set();self.jobs={}
        self.pending='PLATFORM_REFERENCE_PENDING'
        self.available=all(callable(getattr(ctx,k,None)) for k in ('register_platform_handler','spawn_task','on_unload'))

    def wire(self,native,adapter):
        """Connect-time public factory: retain references, assign no adapter fields."""
        if self.closed:return
        if getattr(self.ctx,'profile_name',None)!=self.profile:
            self.adapter=None;self.epoch+=1;self.pending='PLATFORM_ROUTE_UNVERIFIED';return
        if (getattr(getattr(adapter,'platform',None),'value',None)!='weixin'
                or not callable(getattr(adapter,'send_image_file',None))
                or not callable(getattr(adapter,'send',None))):
            self.adapter=None;self.epoch+=1;self.pending='PLATFORM_API_UNAVAILABLE';return
        try:loop=asyncio.get_running_loop()
        except RuntimeError:
            self.pending='PLATFORM_LOOP_UNAVAILABLE';return
        if adapter is not self.adapter:self.epoch+=1
        self.adapter,self.loop=adapter,loop
        self.pending='READY'

    def wire_catalog(self,native,adapter):
        """Public binding for catalog-capable command handling.

        Hermes' public rewire API can attach this newly added factory to the
        existing connection. No private wired-factory set is modified.
        """
        self.wire(native,adapter)

    def close(self):
        # Unload may run on Hermes' discovery thread. Invalidate immediately;
        # cancel on the owning loop. Physical executor work retains its ledger
        # lease and owner object until its real Future finishes.
        self.closed=True;self.adapter=None;self.epoch+=1
        if self.loop is not None and not self.loop.is_closed():
            for task in tuple(self.tasks):self.loop.call_soon_threadsafe(task.cancel)

    def capability(self):
        return {'commands':'ZERO_MODEL_AFTER_ADMISSION','adapter':self.pending if not self.closed else 'UNLOADED',
                'repeatedInbound':'HERMES_OWNED_NOT_GUARANTEED','busyBypass':'UNSUPPORTED','qualified':False}

    def _same_profile(self):
        # Dedicated profile adapters are supported. A routed runtime must not
        # quietly borrow another profile's connection or ambient secrets.
        if getattr(self.ctx,'profile_name',None)!=self.profile:
            raise BridgeError('PLATFORM_ROUTE_UNVERIFIED')

    def accept(self,source,message_id,session_key,platform,text):
        command=parse(text)
        if command is None:return None
        bind_inbound(self.profile,'',{},'','')
        try:
            item=identity(self.profile,source,message_id,session_key,platform)
            self._same_profile()
            bridge=Bridge.from_file(self.config_path,authorize_download=True)
            bridge.principal(item)
            action,args=command
            if action=='invalid' or (action=='capture' and len(args)>5):
                return handled('COMMAND_INVALID','请发送 0–9、BTC1 或 截图1；单条最多 5 项。')
            deliveries=Deliveries(bridge)
            if action=='status':return handled('RECORDED_STATUS',deliveries.status(item))
            if not self.available:return handled('PLUGIN_API_UNAVAILABLE','Hermes 插件接口尚未就绪，未提交截图。')
            if self.closed:return handled('PLUGIN_UNLOADED','插件正在卸载，未提交截图。')
            if self.adapter is None:return handled(self.pending,'尚未绑定本 Profile 的现有微信连接；请确认空闲后重启现有 Gateway 一次。')
            if self.loop is not asyncio.get_running_loop():raise BridgeError('PLATFORM_LOOP_UNAVAILABLE')
            if not bool(self.adapter.is_connected):raise BridgeError('PLATFORM_DISCONNECTED')
            if action=='help':
                coro=self._help(bridge,item,self.adapter,self.epoch)
                try:task=self.ctx.spawn_task(coro,name='asbot-template-help')
                except Exception:
                    coro.close();raise BridgeError('TASK_NOT_STARTED') from None
                self.tasks.add(task);task.add_done_callback(self.tasks.discard)
                return handled('COMMAND_HELP','正在读取你可用的模板说明；不调用模型、不提交截图。')
            entries=[(item if len(args)==1 else replace(item,message_id='batch:'+hashlib.sha256(item.message_id.encode()).hexdigest()+':'+str(n)),shortcut)
                     for n,shortcut in enumerate(args)]
            keys=deliveries.claim_many(entries)
            for index,(key,(entry,shortcut)) in enumerate(zip(keys,entries)):
                job={'key':key,'identity':entry,'shortcut':shortcut,'bridge':bridge,'deliveries':deliveries,
                     'adapter':self.adapter,'epoch':self.epoch,'deadline':time.monotonic()+JOB_SECONDS,
                     'physical':None,'cancelled':False,'ended':False,'started':False,'sending':False}
                self.jobs[key]=job
                coro=self._run(job)
                try:task=self.ctx.spawn_task(coro,name='asbot-public-command')
                except Exception:
                    coro.close();self.jobs.pop(key,None)
                    for remaining in keys[index:]:deliveries.update(remaining,'failed',error='TASK_NOT_STARTED')
                    raise BridgeError('TASK_NOT_STARTED') from None
                self.tasks.add(task)
                task.add_done_callback(lambda done,owned=job:self._ended(done,owned))
            return handled('COMMAND_ACCEPTED','截图指令已记录，按顺序处理并原生回图；不调用模型。')
        except BridgeError as error:
            code=error.code if re.fullmatch(r'[A-Z0-9_]{1,80}',error.code) else 'COMMAND_UNAVAILABLE'
            return handled(code,{'DIRECT_ALREADY_RECORDED':'原消息已记录，不重复截图或发送。',
                         'DIRECT_QUEUE_FULL':'截图队列已满，未提交新任务；请先核对已有任务。'}.get(code,'指令未获准或尚未就绪，未切换到模型；请保留原任务核对。'))
        except Exception:
            return handled('COMMAND_UNAVAILABLE','截图入口暂不可用，未切换到模型；请核对已有任务。')

    async def _help(self,bridge,item,adapter,epoch):
        # GET-only work stays outside the admission hook. The fixed authenticated
        # API already filters this catalog to the person's permitted templates.
        job={'identity':item,'bridge':bridge,'adapter':adapter,'epoch':epoch,'cancelled':False}
        def read():
            try:
                catalog=bridge.service_get(item,'screenshot-templates')
                rows=catalog.get('templates')
                if not isinstance(rows,list):raise BridgeError('INVALID_API_RESPONSE')
                lines=[];seen=set()
                valid=[r for r in rows if isinstance(r,dict) and re.fullmatch(r'[0-9]',str(r.get('shortcut','')))]
                for row in sorted(valid,key=lambda r:int(r['shortcut']))[:10]:
                    if not isinstance(row,dict) or not re.fullmatch(r'[0-9]',str(row.get('shortcut',''))):continue
                    shortcut=str(row['shortcut'])
                    if shortcut in seen:continue
                    seen.add(shortcut)
                    lines.append(template_text(shortcut,row.get('description') or row.get('name'))+
                                 ('（暂不可用）' if row.get('available') is False else ''))
                bridge.authorize_delivery(item)
                return HELP+'\n\n可用模板：\n'+('\n'.join(lines) or '当前没有可用模板。')
            finally:
                close=getattr(bridge.transport,'close',None)
                if callable(close):close()
        future=asyncio.get_running_loop().run_in_executor(None,copy_context().run,read)
        future.add_done_callback(lambda done:None if done.cancelled() else done.exception())
        try:
            text=await asyncio.wait_for(asyncio.shield(future),15)
            self._active(job)
            await asyncio.wait_for(adapter.send(chat_id=item.chat_id,content=text),15)
        except asyncio.CancelledError:raise
        except Exception:
            # No model fallback, no cached/other-person catalog, no retry send.
            return

    def _active(self,job):
        self._same_profile()
        if self.closed or job['cancelled'] or job['epoch']!=self.epoch or self.adapter is not job['adapter']:
            raise BridgeError('PLATFORM_GENERATION_CHANGED')
        if not bool(job['adapter'].is_connected):raise BridgeError('PLATFORM_DISCONNECTED')
        job['bridge'].principal(job['identity'])

    def _remaining(self,job):
        remaining=job['deadline']-time.monotonic()
        if remaining<=0:raise TimeoutError()
        return remaining

    def _notice_active(self,job):
        # A task deadline may have expired, but a failure notice is still allowed
        # in this generation. Unload/reconnect/revocation are never bypassed.
        self._same_profile()
        if self.closed or job['epoch']!=self.epoch or self.adapter is not job['adapter']:
            raise BridgeError('PLATFORM_GENERATION_CHANGED')
        if not bool(job['adapter'].is_connected):raise BridgeError('PLATFORM_DISCONNECTED')
        job['bridge'].principal(job['identity'],job.get('token'))

    def _notice_record(self,job,state,code):
        try:
            with job['bridge'].ledger.connect() as db:
                db.execute('CREATE TABLE IF NOT EXISTS direct_delivery_notices(key TEXT PRIMARY KEY,state TEXT NOT NULL,error_code TEXT NOT NULL)')
                db.execute('INSERT OR REPLACE INTO direct_delivery_notices VALUES(?,?,?)',(job['key'],state,code))
        except Exception:pass  # the original delivery row remains authoritative

    async def _notify_failure(self,job,code):
        if job.get('notice_attempted'):return
        job['notice_attempted']=True
        code=code if isinstance(code,str) and re.fullmatch(r'[A-Z0-9_]{1,80}',code) else 'COMMAND_FAILED'
        self._notice_record(job,'attempted',code)
        bridge=None
        try:
            self._notice_active(job)
            # A timed-out capture thread can still own its transport lock. Use a
            # separate read-only authorization connection; never resubmit capture.
            bridge=Bridge.from_file(self.config_path)
            entry=job['identity'];token=job.get('token')
            deadline=time.monotonic()+NOTICE_SECONDS
            def authorize():
                self._notice_active(job)
                return bridge.authorize_delivery(entry,token)
            future=asyncio.get_running_loop().run_in_executor(None,copy_context().run,authorize)
            def finished(done):
                try:done.exception()
                except BaseException:pass
                try:bridge.transport.close()
                except Exception:pass
            future.add_done_callback(finished)
            await asyncio.wait_for(asyncio.shield(future),max(.001,deadline-time.monotonic()))
            self._notice_active(job)
            remaining=deadline-time.monotonic()
            if remaining<=0:raise TimeoutError()
            reply=('BTC'+job['shortcut']+' 本次请求未完成或图片送达待核实 ['+code+']。\n'
                   '请发“截图状态”并联系维护者核对原任务；不要自动换新编号重试，系统不会自动重发图片。')
            response=await asyncio.wait_for(job['adapter'].send(chat_id=entry.chat_id,content=reply),remaining)
            self._notice_record(job,'sent' if response is not None and getattr(response,'success',False) is True else 'failed',code)
        except asyncio.CancelledError:
            self._notice_record(job,'cancelled',code)
            raise
        except Exception:
            # One public send attempt only; no exception body, credential, path
            # or native response is echoed, and notification failure never retries.
            self._notice_record(job,'failed',code)

    async def _physical(self,job,call):
        self._active(job)
        job['deliveries'].update(job['key'],'capturing',physical_pending=1)
        def owned_work():
            self._active(job)  # an executor-queued call may start after unload
            return call()
        try:future=asyncio.get_running_loop().run_in_executor(None,copy_context().run,owned_work)
        except Exception:
            job['deliveries'].update(job['key'],'unknown',physical_pending=0,error='EXECUTOR_UNAVAILABLE')
            raise
        job['physical']=future
        future.add_done_callback(lambda done:self._physical_done(job,done))
        return await asyncio.wait_for(asyncio.shield(future),self._remaining(job))

    def _physical_done(self,job,future):
        # Retrieving an exception also prevents an abandoned Future warning.
        try:future.exception()
        except BaseException:pass
        if job['physical'] is not future:return  # never clear a newer physical lease
        job['physical']=None
        state='unknown' if self.closed or job['cancelled'] or job['ended'] else 'capturing'
        try:
            job['deliveries'].update(job['key'],state,physical_pending=0,
                                     error='PHYSICAL_WORK_ENDED' if state=='unknown' else None)
        except Exception:pass  # preserve the existing lease/evidence on local I/O failure
        if job['ended']:self.jobs.pop(job['key'],None)

    def _ended(self,task,job):
        self.tasks.discard(task);job['ended']=True
        try:task.exception()
        except BaseException:pass
        try:job['deliveries'].task_ended(job['key'])
        except Exception:pass
        if job['physical'] is None:self.jobs.pop(job['key'],None)

    async def _run(self,job):
        bridge,entry,deliveries,key=job['bridge'],job['identity'],job['deliveries'],job['key']
        try:
            token=bridge.principal(entry)['bearer_token']
            job['token']=token
            while not deliveries.turn(key):
                self._active(job);self._remaining(job)
                await asyncio.sleep(min(.1,self._remaining(job)))
            # Bound both Bridge's admission wait and result polling; the overall
            # task deadline also includes the public send. Cancelling the await
            # never claims the physical executor has stopped.
            bridge.wait_seconds=min(180,max(0,(self._remaining(job)-SEND_SECONDS)/2))
            await self._physical(job,lambda:bridge.authorize_delivery(entry,token))
            job['started']=True
            result=await self._physical(job,lambda:bridge.invoke(entry,'screenshot.capture',{'shortcut':job['shortcut']}))
            self._active(job)
            if not result.get('ok') or not result.get('image') or result.get('duplicate'):
                raise BridgeError('CAPTURE_NOT_DELIVERABLE')
            image=Path(result['image'])
            if image.parent!=bridge.artifacts or not re.fullmatch(r'[a-f0-9]{32}\.png',image.name):raise BridgeError('INVALID_ARTIFACT')
            private_file(image);validate_png(image.read_bytes())
            await self._physical(job,lambda:bridge.authorize_delivery(entry,token))
            self._active(job);bridge.principal(entry,token)
            deliveries.update(key,'sending',image=str(image));job['sending']=True
            response=await asyncio.wait_for(job['adapter'].send_image_file(chat_id=entry.chat_id,image_path=str(image),
                                            caption=template_text(job['shortcut'],result.get('label'))),
                                            min(SEND_SECONDS,self._remaining(job)))
            if response is not None and getattr(response,'success',False) is True:
                deliveries.update(key,'sent')
            else:
                deliveries.update(key,'unknown',error='SEND_OUTCOME_UNKNOWN')
                await self._notify_failure(job,'SEND_OUTCOME_UNKNOWN')
        except (asyncio.CancelledError,TimeoutError):
            job['cancelled']=True
            deliveries.update(key,'unknown',error='COMMAND_CANCELLED_OR_TIMED_OUT')
            if asyncio.current_task().cancelling():raise
            await self._notify_failure(job,'COMMAND_CANCELLED_OR_TIMED_OUT')
        except Exception as error:
            code=error.code if isinstance(error,BridgeError) else 'COMMAND_FAILED'
            deliveries.update(key,'unknown' if job['started'] or job['sending'] or job['physical'] is not None else 'failed',error=code)
            await self._notify_failure(job,code)
