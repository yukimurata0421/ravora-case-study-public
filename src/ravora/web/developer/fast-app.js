const $=id=>document.getElementById(id),diagnostics=window.ravoraTalkDiagnostics={profile:'fast_streaming',recordings:0,liveMicTracks:0,playedAcks:0,pcmPackets:0,turns:[],socketErrors:0,reactions:[],expression:'neutral'};
const key=new URLSearchParams(location.hash.slice(1)).get('token');history.replaceState(null,'',location.pathname);
let ws,cfg,busy=false,epoch=0,held=false,mic=null,ctx,node,meter,current=null,inputId=null,startAt=0,inputEnd=null,assistant=null,timer=null,preparing=false;
let reactionLocked=true,reactionSequence=0;
const uuid=()=>crypto.randomUUID().replaceAll('-','');
function send(value){if(ws?.readyState!==WebSocket.OPEN)throw Error('Connection closed');ws.send(JSON.stringify(value));}
function status(text){$('status').textContent=text;}
function resetReaction(){reactionLocked=true;diagnostics.expression='neutral';$('reaction').textContent='Reaction: neutral';$('interim').textContent='';}
function beginReaction(){resetReaction();reactionSequence=0;reactionLocked=false;}
function controls(){const connected=Boolean(cfg&&ws?.readyState===WebSocket.OPEN),ready=connected&&!busy;$('talk').disabled=!connected||(busy&&!held);$('send').disabled=!ready;$('language').disabled=!ready;}
function message(role,text){const item=document.createElement('div');item.className='message '+role;const label=document.createElement('small');label.textContent=role==='user'?'YOU':'RAVORA';const body=document.createElement('span');body.textContent=text;item.append(label,body);$('messages').append(item);item.scrollIntoView({block:'nearest'});return body;}
async function output(){
  if(!ctx){
    ctx=new AudioContext({sampleRate:24000,latencyHint:'interactive'});await Promise.all([ctx.audioWorklet.addModule('/pcm-playout.js'),ctx.audioWorklet.addModule('/latency-meter.js')]);
    if(ctx.sampleRate!==24000)throw Error('24 kHz playback is unavailable');
    node=new AudioWorkletNode(ctx,'ravora-pcm-playout',{numberOfInputs:0,outputChannelCount:[1]});
    meter=new AudioWorkletNode(ctx,'ravora-latency-meter',{outputChannelCount:[1]});node.connect(ctx.destination);node.connect(meter);meter.connect(ctx.destination);
    meter.port.onmessage=({data})=>{if(current?.token.turn_id===data.turn_id){current.first_audio_signal_ms=performance.now()-startAt;current.from_input_end_ms=inputEnd===null?null:performance.now()-inputEnd;}};
    node.port.onmessage=({data})=>{
      if(data.kind==='error'){fail('Playback buffer limit reached');return;}
      if(data.kind==='ended'&&current?.token.turn_id===data.turn_id){
        Object.assign(current,{frames:data.frames,underflow_frames:data.underflow_frames,ended:true});resetReaction();ack();
      }
    };
  }
  await ctx.resume();
}
function ack(){if(current?.ended&&current?.complete&&!current.ackSent){current.ackSent=true;send({kind:'played',token:current.token,frames:current.frames});}}
function discard(){current=null;node?.port.postMessage({kind:'reset'});meter?.port.postMessage({});assistant=null;}
async function stopMic(flush=false){
  clearTimeout(timer);timer=null;const recording=mic;mic=null;held=false;$('talk').classList.remove('recording');
  if(!recording)return;
  try{
    if(flush)await Promise.race([new Promise(resolve=>{recording.done=resolve;recording.node.port.postMessage({kind:'stop'});}),new Promise((_,reject)=>setTimeout(()=>reject(Error('Could not finish recording')),1500))]);
  }finally{
    recording.stream.getTracks().forEach(track=>track.stop());diagnostics.liveMicTracks=0;
    recording.source.disconnect();recording.node?.disconnect();await recording.ctx.close();
  }
}
async function stop(){epoch++;held=false;preparing=false;discard();resetReaction();await stopMic();try{send({kind:'cancel'});}catch{}busy=false;controls();status('Stopped');}
async function fail(text){await stop();status(text);}
async function begin(){
  if(busy||preparing||!cfg)return;held=true;preparing=true;busy=true;controls();const version=++epoch;
  try{
    await output();if(!held||version!==epoch)return;
    const stream=await navigator.mediaDevices.getUserMedia({audio:{channelCount:1,echoCancellation:true,noiseSuppression:true,autoGainControl:true},video:false});
    if(!held||version!==epoch){stream.getTracks().forEach(track=>track.stop());return;}
    const context=new AudioContext({sampleRate:16000,latencyHint:'interactive'});
    const recording=mic={ctx:context,stream,source:context.createMediaStreamSource(stream),node:null,version,done:null};diagnostics.liveMicTracks=stream.getAudioTracks().length;
    await context.audioWorklet.addModule('/mic-stream.js');if(version!==epoch){await stopMic();return;}
    if(context.sampleRate!==16000)throw Error('16 kHz recording is unavailable');
    const worklet=recording.node=new AudioWorkletNode(context,'ravora-mic-stream',{outputChannelCount:[1],processorOptions:{chunk_ms:cfg.input_chunk_ms}});
    inputId=uuid();beginReaction();recording.inputId=inputId;startAt=performance.now();inputEnd=null;diagnostics.recordings++;
    worklet.port.onmessage=({data})=>{
      if(data.kind==='done'){recording.done?.();return;}
      if(version!==epoch||data.kind!=='pcm')return;
      const packet=new Uint8Array(16+data.samples.length*2),view=new DataView(packet.buffer);for(let i=0;i<16;i++)packet[i]=parseInt(recording.inputId.slice(i*2,i*2+2),16);
      for(let i=0;i<data.samples.length;i++)view.setInt16(16+i*2,data.samples[i],true);
      if(ws.readyState===WebSocket.OPEN&&ws.bufferedAmount<128000)ws.send(packet);else fail('Stopped because upload fell behind');
    };
    send({kind:'audio_start',input_id:inputId,language:$('language').value});
    // Microphone starts only after the server accepts this input ID.
    status('Connecting microphone...');preparing=false;
  }catch(error){if(version===epoch)await fail(error.message||'Could not start microphone');}
  finally{if(version===epoch)preparing=false;}
}
async function end(){
  if(!held&&!preparing)return;held=false;
  if(preparing||!mic?.started){await stop();return;}
  const version=epoch,id=mic.inputId;
  try{await stopMic(true);if(version!==epoch)return;inputEnd=performance.now();send({kind:'audio_end',input_id:id});status('Waiting for a reply...');}catch(error){await fail(error.message);}
}
$('talk').addEventListener('pointerdown',event=>{event.preventDefault();$('talk').setPointerCapture(event.pointerId);begin();});
$('talk').addEventListener('pointerup',end);$('talk').addEventListener('pointercancel',stop);
$('talk').addEventListener('keydown',event=>{if(['Space','Enter'].includes(event.code)&&!event.repeat){event.preventDefault();begin();}});
$('talk').addEventListener('keyup',event=>{if(['Space','Enter'].includes(event.code)){event.preventDefault();end();}});
$('stop').addEventListener('click',stop);
$('form').addEventListener('submit',async event=>{
  event.preventDefault();const text=$('text').value.trim();if(!text||busy||!cfg)return;busy=true;controls();epoch++;discard();
  try{await output();inputId=uuid();beginReaction();startAt=performance.now();inputEnd=startAt;send({kind:'text',input_id:inputId,text});$('text').value='';status('Waiting for a reply...');}catch(error){await fail(error.message);}
});
window.addEventListener('beforeunload',()=>{mic?.stream.getTracks().forEach(track=>track.stop());ws?.close();});
window.addEventListener('blur',()=>{if(held)end();});
async function connect(){
  if(!key){status('Open the URL printed at startup');return;}
  const {port}=await (await fetch('/connection')).json();ws=new WebSocket(`ws://127.0.0.1:${port}/voice?token=${encodeURIComponent(key)}`);ws.binaryType='arraybuffer';
  ws.onclose=()=>{cfg=null;diagnostics.socketErrors++;stopMic();discard();resetReaction();busy=true;controls();status('Session ended. Restart to open a new session.');};
  ws.onerror=()=>status('Could not connect');
  ws.onmessage=async({data})=>{
    if(data instanceof ArrayBuffer){
      if(!current||data.byteLength<18)return;const bytes=new Uint8Array(data),id=Array.from(bytes.slice(0,16),v=>v.toString(16).padStart(2,'0')).join('');if(id!==current.token.turn_id)return;
      const view=new DataView(data),samples=new Float32Array((data.byteLength-16)/2);for(let i=0;i<samples.length;i++)samples[i]=view.getInt16(16+i*2,true)/32768;
      node.port.postMessage({kind:'pcm',samples},[samples.buffer]);diagnostics.pcmPackets++;return;
    }
    const value=JSON.parse(data);
    if(value.kind==='ready'){
      cfg=value.config;$('language').value=cfg.input_language;$('mode').textContent=cfg.mock?'Offline simulation':'Voice conversation';
      $('notice').textContent=cfg.mock?'Offline simulation: microphone transcription is canned and reply audio is a test tone.':'Cloud mode requires explicit operator configuration.';
      $('limit').textContent=`Up to ${cfg.max_seconds} seconds per recording. Stop cancels recording and playback.`;busy=false;controls();status('Ready');return;
    }
    if(value.input_id&&value.input_id!==inputId)return;
    if(value.kind==='reaction'){
      if(reactionLocked||!busy||value.input_id!==inputId)return;
      const rank=value.phase==='partial'?1:value.phase==='final'?2:0;
      if(!rank||value.reaction_sequence!==rank||rank<=reactionSequence)return;
      if(value.token&&value.token.turn_id!==current?.token.turn_id)return;
      if(rank===2&&(!current||current.token.turn_id!==value.token?.turn_id))return;
      const labels={neutral:'neutral',joy:'friendly',surprise:'surprised',concerned:'concerned'};
      const next=Object.hasOwn(labels,value.reaction)?value.reaction:'neutral';
      const previous=diagnostics.expression;
      reactionSequence=rank;
      if(previous!==next){
        diagnostics.expression=next;
        $('reaction').textContent='Reaction: '+labels[next];
      }
      diagnostics.reactions.push({input_id:inputId,phase:value.phase,reaction:next,reaction_sequence:rank,
        transition:previous===next?'continue':next==='neutral'?'return_neutral':'switch',
        while_listening:Boolean(mic?.started),before_turn:!current});
      return;
    }
    if(value.kind==='reaction_reset'){resetReaction();return;}
    if(value.kind==='stt_partial'){$('interim').textContent=(cfg.mock?'Simulated partial transcript: ':'Partial transcript: ')+value.text;return;}
    if(value.kind==='listening'){
      if(!held||!mic||mic.inputId!==value.input_id)return;
      mic.source.connect(mic.node);mic.node.connect(mic.ctx.destination);await mic.ctx.resume();mic.started=true;
      $('talk').classList.add('recording');status('Listening...');timer=setTimeout(end,cfg.max_seconds*1000-200);return;
    }
    if(value.kind==='turn'){
      $('interim').textContent='';
      discard();current={token:value.token,input_id:inputId,complete:false,ended:false};message('user',value.text);assistant=message('assistant','');
      node.port.postMessage({kind:'reset',turn_id:value.token.turn_id,jitter_ms:cfg.jitter_buffer_ms});meter.port.postMessage({turn_id:value.token.turn_id,not_before:ctx.currentTime});return;
    }
    if(value.kind==='text'&&current?.token.turn_id===value.token.turn_id){assistant.textContent+=value.text;$('messages').scrollTop=$('messages').scrollHeight;status('Replying...');}
    if(value.kind==='audio_end'&&current?.token.turn_id===value.token.turn_id){current.expectedFrames=value.frames;node.port.postMessage({kind:'finish'});}
    if(value.kind==='complete'&&current?.token.turn_id===value.token.turn_id){current.complete=true;current.metrics=value.metrics;ack();}
    if(value.kind==='played_ack'&&current?.token.turn_id===value.token.turn_id){
      if(!value.accepted){await fail('Playback acknowledgement failed');return;}
      diagnostics.playedAcks++;diagnostics.turns.push({...current});current=null;busy=false;controls();status('Ready');
    }
    if(value.kind==='error'){await fail(`Stopped: ${value.code}`);}
    if(value.kind==='cancelled'){discard();await stopMic();busy=false;controls();status(value.code==='NO_SPEECH'?'No speech detected':'Stopped');}
  };
}
connect().catch(()=>status('Could not connect'));
