"""Extracted input ledger and stable validation errors."""
import datetime as dt
import hashlib,json,re,time

class DeveloperError(ValueError):pass


class InputRecords:
    def __init__(self,store,session,mode,*,reaction_limit=1):
        if type(reaction_limit) is not int or reaction_limit not in (1,2):raise DeveloperError('INVALID_REACTION_LIMIT')
        self.store=store;self.session=session;self.mode=mode;self.reaction_limit=reaction_limit

    def create(self,client_id,kind,payload):
        if not isinstance(client_id,str) or not re.fullmatch('[a-f0-9]{32}',client_id):raise DeveloperError('INVALID_INPUT_ID')
        digest=hashlib.sha256(payload).hexdigest();key=hashlib.sha256((self.session+'\0'+client_id).encode()).hexdigest();now=time.time()
        with self.store.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            old=db.execute('SELECT kind,payload_sha256 FROM developer_inputs WHERE id=?',(key,)).fetchone()
            if old:
                if old!=(kind,digest):raise DeveloperError('INPUT_ID_REUSED')
                return key,False
            month=dt.datetime.now(dt.timezone(dt.timedelta(hours=9))).strftime('%Y-%m')
            columns=',reaction_limit' if self.reaction_limit==2 else ''
            extra=',2' if self.reaction_limit==2 else ''
            db.execute('INSERT INTO developer_inputs(id,session,client_id,kind,payload_sha256,mode,'
                'created_at,updated_at,month,input_state'+columns+") VALUES(?,?,?,?,?,?,?,?,?,'prepared'"+extra+')',
                (key,self.session,client_id,kind,digest,self.mode,now,now,month))
        return key,True

    def state(self,key,state,code=None,*,language=None,source=None):
        with self.store.connect() as db:
            db.execute('''UPDATE developer_inputs SET input_state=?,code=?,updated_at=?,language=COALESCE(?,language),
                source_event_id=COALESCE(?,source_event_id) WHERE id=? AND session=? AND input_state!='cancelled' ''',
                (state,code,time.time(),language,source,key,self.session))

    def prepare_audio(self,key,packet,settings,provider,model=None):
        with self.store.connect() as db:db.execute('UPDATE developer_inputs SET duration_seconds=?,provider=?,model=? WHERE id=?',
            (packet.duration,provider,model or settings.stt_model,key))

    def dispatch_local(self,key,settings):
        with self.store.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            row=db.execute("SELECT month,duration_seconds FROM developer_inputs WHERE id=? AND input_state='prepared' AND local_attempted=0",(key,)).fetchone()
            if not row:return False
            count=db.execute('SELECT COUNT(*) FROM developer_inputs WHERE session=? AND local_attempted=1',(self.session,)).fetchone()[0]
            used=db.execute('SELECT COALESCE(SUM(duration_seconds),0) FROM developer_inputs WHERE month=? AND local_attempted=1',(row[0],)).fetchone()[0]
            code='STT_SESSION_LIMIT' if count>=settings.stt_session_requests else 'STT_MONTHLY_AUDIO_LIMIT' if used+row[1]>settings.stt_monthly_audio_seconds else None
            if code:
                db.execute("UPDATE developer_inputs SET input_state='held',code=?,updated_at=? WHERE id=?",(code,time.time(),key));return False
            db.execute("UPDATE developer_inputs SET input_state='processing',local_attempted=1,updated_at=? WHERE id=?",(time.time(),key))
        return True

    def dispatch(self,key,settings):
        with self.store.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            row=db.execute("SELECT month,duration_seconds FROM developer_inputs WHERE id=? AND input_state='prepared' AND request_attempted=0",(key,)).fetchone()
            if not row:return False
            count=db.execute('SELECT COUNT(*) FROM developer_inputs WHERE session=? AND request_attempted=1',(self.session,)).fetchone()[0]
            used=db.execute('SELECT COALESCE(SUM(duration_seconds),0) FROM developer_inputs WHERE month=? AND request_attempted=1',(row[0],)).fetchone()[0]
            if count>=settings.stt_session_requests:code='STT_SESSION_LIMIT'
            elif used+row[1]>settings.stt_monthly_audio_seconds:code='STT_MONTHLY_AUDIO_LIMIT'
            else:code=None
            if code:
                db.execute("UPDATE developer_inputs SET input_state='held',code=?,updated_at=? WHERE id=?",(code,time.time(),key));return False
            db.execute("UPDATE developer_inputs SET input_state='dispatched',request_attempted=1,updated_at=? WHERE id=?",(time.time(),key))
        return True

    def usage(self,key,usage,elapsed):
        fields=('input_tokens','output_tokens','thought_tokens','total_tokens')
        values=[usage.get(f) if type(usage.get(f)) is int and usage[f]>=0 else None for f in fields]
        model=usage.get('response_model')
        if not isinstance(model,str) or not re.fullmatch('[a-zA-Z0-9._/-]{1,160}',model):model=None
        with self.store.connect() as db:db.execute('''UPDATE developer_inputs SET input_tokens=?,output_tokens=?,thought_tokens=?,
            total_tokens=?,response_model=?,latency_ms=? WHERE id=?''',(*values,model,max(0,elapsed),key))
        diagnostics=usage.get('local_diagnostics',{})
        encoded=json.dumps(diagnostics,allow_nan=False)
        if len(encoded)>128*1024:raise DeveloperError('STT_DIAGNOSTICS_TOO_LARGE')
        with self.store.connect() as db:db.execute('UPDATE developer_inputs SET stt_diagnostics=? WHERE id=?',(encoded,key))

    def info(self,key):
        with self.store.connect() as db:
            row=db.execute('''SELECT d.client_id,d.kind,d.input_state,d.code,d.duration_seconds,d.request_attempted,
                d.language,s.text,t.turn_id,t.status,t.played,t.reply,d.provider,d.model,d.local_attempted,
                d.latency_ms,d.stt_diagnostics FROM developer_inputs d
                LEFT JOIN source_events s ON s.id=d.source_event_id
                LEFT JOIN conversation_turns t ON t.source_event_id=d.source_event_id
                WHERE d.id=? AND d.session=?''',(key,self.session)).fetchone()
        if not row:raise DeveloperError('INPUT_NOT_FOUND')
        value=dict(zip(('input_id','kind','input_state','code','duration_seconds','stt_attempted','language',
            'transcript','turn_id','conversation_status','played','reply','stt_provider','stt_model','local_attempted',
            'stt_latency_ms','stt_diagnostics'),row));value['stt_diagnostics']=json.loads(value['stt_diagnostics']);return value

    def from_client(self,client_id):
        if not isinstance(client_id,str) or not re.fullmatch('[a-f0-9]{32}',client_id):raise DeveloperError('INVALID_INPUT_ID')
        return hashlib.sha256((self.session+'\0'+client_id).encode()).hexdigest()
