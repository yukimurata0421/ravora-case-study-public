const $=id=>document.getElementById(id),diagnostics=window.ravoraTalkDiagnostics={profile:'fast_streaming',recordings:0,liveMicTracks:0,playedAcks:0,pcmPackets:0,turns:[],socketErrors:0,reactions:[],expression:'neutral'};
const tabKey='ravora-fast-talk-token';
const fragmentKey=()=>new URLSearchParams(location.hash.slice(1)).get('token');
let key=fragmentKey();
try{
  if(key)sessionStorage.setItem(tabKey,key);
  else key=sessionStorage.getItem(tabKey);
}catch{} // The launch URL also works with browser storage disabled.
history.replaceState(null,'',location.pathname);
window.addEventListener('hashchange',()=>{if(fragmentKey())location.reload();});
let ws,cfg,busy=false,epoch=0,held=false,listening=false,mic=null,ctx,node,meter,current=null,inputId=null,startAt=0,inputEnd=null,assistant=null,timer=null,preparing=false,reactionSeq=0,reactionLocked=false;
let avatar;
let armedNotice='Waiting for speech…';
const stage=$('reaction-preview');
$('fullscreen').addEventListener('click',async()=>{
  try{
    if(document.fullscreenElement)await document.exitFullscreen();
    else{await stage.requestFullscreen();stage.tabIndex=-1;stage.focus();}
  }catch{status('Fullscreen is unavailable in this browser.');}
});
document.addEventListener('fullscreenchange',()=>{$('fullscreen').textContent=document.fullscreenElement?'Exit fullscreen':'Fullscreen';});
const uuid=()=>crypto.randomUUID().replaceAll('-','');
function send(value){if(ws?.readyState!==WebSocket.OPEN)throw Error('Connection closed.');ws.send(JSON.stringify(value));}
function status(text){$('status').textContent=text;$('stage-status').textContent=text;}
function expression(name='neutral'){
  const captions={neutral:'Listening',joy:'Happy',surprise:'Surprised'};if(!(name in captions))name='neutral';
  diagnostics.expression=name;document.querySelectorAll('[data-reaction]').forEach(image=>image.classList.toggle('active',image.dataset.reaction===name));
  $('reaction-caption').textContent=captions[name];$('reaction-preview').setAttribute('aria-label',`Ravora: ${captions[name]}`);
}
document.querySelectorAll('[data-reaction]').forEach(image=>image.addEventListener('error',()=>$('reaction-preview').classList.add('unavailable')));
const handsfree=()=>$('end-mode').value==='handsfree';
const autoEnd=()=>$('end-mode').value!=='manual';
function controls(){const connected=Boolean(cfg&&ws?.readyState===WebSocket.OPEN),ready=connected&&!busy;$('talk').disabled=!connected||(busy&&!held);$('stop').disabled=!connected;$('send').disabled=!ready;$('language').disabled=!ready;$('input-source').disabled=!ready;$('end-mode').disabled=!ready;
  if(handsfree())$('talk').disabled=!ready;
  $('stage-stop').disabled=!connected||!busy;
  $('talk').textContent=handsfree()?(busy?'Hands-free':'Start hands-free'):autoEnd()?(listening?'Send now':'Start speaking'):'Hold to talk';}
function message(role,text){const item=document.createElement('div');item.className='message '+role;const label=document.createElement('small');label.textContent=role==='user'?'YOU':'RAVORA';const body=document.createElement('span');body.textContent=text;item.append(label,body);$('messages').append(item);item.scrollIntoView({block:'nearest'});return body;}
let outputReady;
async function setupOutput(){
  ctx=new AudioContext({sampleRate:24000,latencyHint:'interactive'});await Promise.all([ctx.audioWorklet.addModule('/pcm-playout.js'),ctx.audioWorklet.addModule('/latency-meter.js')]);
  if(ctx.sampleRate!==24000)throw Error('24 kHz playback is unavailable.');
  node=new AudioWorkletNode(ctx,'ravora-pcm-playout',{numberOfInputs:0,outputChannelCount:[1]});
  meter=new AudioWorkletNode(ctx,'ravora-latency-meter',{outputChannelCount:[1]});node.connect(ctx.destination);node.connect(meter);meter.connect(ctx.destination);
  meter.port.onmessage=({data})=>{if(current?.token.turn_id===data.turn_id){current.first_audio_signal_ms=performance.now()-startAt;current.from_input_end_ms=inputEnd===null?null:performance.now()-inputEnd;}};
  node.port.onmessage=({data})=>{
    if(data.kind==='error'){fail('Playback buffer limit reached.');return;}
    if(data.kind==='started'&&current?.token.turn_id===data.turn_id)avatar?.start(ctx,data.context_frame);
    if(data.kind==='ended'&&current?.token.turn_id===data.turn_id){avatar?.stop();Object.assign(current,{frames:data.frames,underflow_frames:data.underflow_frames,ended:true});reactionLocked=true;expression();ack();}
  };
}
async function output(){
  if(!outputReady){
    outputReady=setupOutput().catch(error=>{
      ctx?.close();ctx=null;outputReady=null;throw error;
    });
  }
  await outputReady;
  await ctx.resume();
}
function ack(){if(current?.ended&&current?.complete&&!current.ackSent){current.ackSent=true;send({kind:'played',token:current.token,frames:current.frames});}}
function discard(){current=null;avatar?.stop();node?.port.postMessage({kind:'reset'});meter?.port.postMessage({});assistant=null;}
async function stopMic(flush=false){
  clearTimeout(timer);timer=null;listening=false;const recording=mic;mic=null;held=false;$('talk').classList.remove('recording');
  if(!recording)return;
  try{
    recording.discarded=!flush;
    if(flush)await Promise.race([new Promise(resolve=>{recording.done=resolve;recording.node.port.postMessage({kind:'stop'});}),new Promise((_,reject)=>setTimeout(()=>reject(Error('Could not finish recording.')),1500))]);
  }finally{
    recording.stream.getTracks().forEach(track=>track.stop());diagnostics.liveMicTracks=0;
    recording.source.disconnect();recording.node?.disconnect();await recording.ctx.close();
  }
}
function leaveHandsfree(){if(handsfree())$('end-mode').value='manual';}
async function stop(){leaveHandsfree();epoch++;held=false;preparing=false;inputId=null;reactionSeq=0;reactionLocked=true;expression();discard();await stopMic();try{send({kind:'cancel'});}catch{}busy=false;controls();status('Stopped');}
async function fail(text){await stop();status(text);}
function startInput(source){
  inputId=uuid();startAt=performance.now();inputEnd=null;diagnostics.recordings++;
  send({kind:'audio_start',input_id:inputId,source,language:$('language').value,end_mode:$('end-mode').value});
  status('Connecting the microphone…');
}
async function begin(waitNotice='Waiting for speech…'){
  if(busy||preparing||!cfg)return;held=true;preparing=true;busy=true;inputId=null;reactionSeq=0;reactionLocked=false;expression();controls();status('Preparing the microphone…');const version=++epoch;
  armedNotice=waitNotice;
  try{
    const source=$('input-source').value;
    if(source==='dell'){
      if(!cfg.dell_mic_available)throw Error('No host capture device is configured.');
      if(autoEnd()){
        // The server can finish this turn before a slow first worklet download completes.
        await output();if(!held||version!==epoch)return;
        startInput(source);return;
      }
      const playback=output();startInput(source);preparing=false;await playback;return;
    }
    await output();if(!held||version!==epoch)return;
    const stream=await navigator.mediaDevices.getUserMedia({audio:{channelCount:1,echoCancellation:true,noiseSuppression:true,autoGainControl:true},video:false});
    if(!held||version!==epoch){stream.getTracks().forEach(track=>track.stop());return;}
    const context=new AudioContext({sampleRate:16000,latencyHint:'interactive'});
    const recording=mic={ctx:context,stream,source:context.createMediaStreamSource(stream),node:null,version,done:null};diagnostics.liveMicTracks=stream.getAudioTracks().length;
    await context.audioWorklet.addModule('/mic-stream.js');if(version!==epoch)return;
    if(context.sampleRate!==16000)throw Error('16 kHz capture is unavailable.');
    const worklet=recording.node=new AudioWorkletNode(context,'ravora-mic-stream',{outputChannelCount:[1],processorOptions:{chunk_ms:cfg.input_chunk_ms}});
    if(!held||version!==epoch)return;
    startInput(source);recording.inputId=inputId;
    worklet.port.onmessage=({data})=>{
      if(data.kind==='done'){recording.done?.();return;}
      if(recording.discarded||version!==epoch||data.kind!=='pcm')return;
      const packet=new Uint8Array(16+data.samples.length*2),view=new DataView(packet.buffer);for(let i=0;i<16;i++)packet[i]=parseInt(recording.inputId.slice(i*2,i*2+2),16);
      for(let i=0;i<data.samples.length;i++)view.setInt16(16+i*2,data.samples[i],true);
      if(ws.readyState===WebSocket.OPEN&&ws.bufferedAmount<128000)ws.send(packet);else fail('Capture stopped because transmission fell behind.');
    };
    preparing=false;
  }catch(error){if(version===epoch)await fail(error.message||'Could not start the microphone.');}
  finally{if(version===epoch)preparing=false;}
}
async function end(){
  if(!held&&!preparing)return;held=false;
  if(preparing||!listening){await stop();return;}
  const version=epoch,id=inputId;
  try{await stopMic(true);if(version!==epoch)return;inputEnd=performance.now();send({kind:'audio_end',input_id:id});status('Waiting for reply…');}catch(error){await fail(error.message);}
}
$('end-mode').addEventListener('change',()=>{controls();if(handsfree())begin();});
$('talk').addEventListener('click',()=>{if(autoEnd()){if(listening)end();else begin();}});
$('talk').addEventListener('pointerdown',event=>{if(autoEnd())return;event.preventDefault();$('talk').setPointerCapture(event.pointerId);begin();});
$('talk').addEventListener('pointerup',()=>{if(!autoEnd())end();});$('talk').addEventListener('pointercancel',()=>{if(!autoEnd())stop();});
let holdKey=null;
window.addEventListener('keydown',event=>{
  if(event.code==='Escape'&&handsfree()){stop();return;}
  if(autoEnd()||!cfg||event.isComposing||event.altKey||event.ctrlKey||event.metaKey||event.shiftKey)return;
  if(event.code!=='Space'&&!(event.code==='Enter'&&event.target===$('talk')))return;
  if(event.target!==$('talk')&&(event.target.isContentEditable||event.target.closest?.('input,textarea,select,button,a,[role="textbox"]')))return;
  event.preventDefault();
  if(!event.repeat&&!busy&&!holdKey){holdKey=event.code;begin();}
});
window.addEventListener('keyup',event=>{if(holdKey&&event.code===holdKey){holdKey=null;event.preventDefault();end();}});
$('stop').addEventListener('click',stop);
$('stage-stop').addEventListener('click',stop);
$('form').addEventListener('submit',async event=>{
  event.preventDefault();const text=$('text').value.trim();if(!text||busy||!cfg)return;busy=true;inputId=null;reactionSeq=0;reactionLocked=false;expression();controls();epoch++;discard();
  try{await output();inputId=uuid();startAt=performance.now();inputEnd=startAt;send({kind:'text',input_id:inputId,text});$('text').value='';status('Waiting for reply…');}catch(error){await fail(error.message);}
});
window.addEventListener('beforeunload',()=>{mic?.stream.getTracks().forEach(track=>track.stop());ws?.close();});
window.addEventListener('blur',()=>{holdKey=null;if(handsfree())stop();else if(held){if(autoEnd())stop();else end();}});
document.addEventListener('visibilitychange',()=>{if(document.hidden&&handsfree())stop();});
function connectionProblem(message,retry){
  $('mode').textContent='Disconnected';$('notice').textContent=message;
  status(message);controls();$('reconnect').hidden=!retry;
}
const openLaunchUrl='Open the latest launch URL with its complete token fragment.';
$('reconnect').addEventListener('click',()=>{
  $('reconnect').disabled=true;
  // Restore the fragment for this navigation even if tab storage was unavailable.
  history.replaceState(null,'','#'+new URLSearchParams({token:key}));
  location.reload();
});
async function connect(){
  if(!key){connectionProblem(openLaunchUrl,false);return;}
  const options={headers:{Authorization:'Bearer '+key},signal:AbortSignal.timeout(10000)};
  let response=await fetch('/connection',options);
  if(response.status===409){
    // A page refresh can arrive before the previous document finishes closing its socket.
    await new Promise(resolve=>setTimeout(resolve,350));
    response=await fetch('/connection',options);
  }
  if(response.status===401){
    try{sessionStorage.removeItem(tabKey);}catch{}
    connectionProblem(openLaunchUrl,false);return;
  }
  const occupied='Another tab is connected. Use that tab or close it and reconnect here.';
  if(response.status===409){connectionProblem(occupied,true);return;}
  if(!response.ok)throw Error('Could not obtain the connection details.');
  const {port}=await response.json();
  ws=new WebSocket(`ws://127.0.0.1:${port}/voice?token=${encodeURIComponent(key)}`);ws.binaryType='arraybuffer';
  const deadline=setTimeout(()=>ws.close(),10000);
  ws.onclose=event=>{
    clearTimeout(deadline);leaveHandsfree();holdKey=null;epoch++;preparing=false;cfg=null;inputId=null;expression();diagnostics.socketErrors++;stopMic();discard();busy=true;
    const message=event.reason==='OPERATOR_ALREADY_CONNECTED'?occupied:
      `Voice connection closed. Check the connection to port ${port}, close any other console tab and select Reconnect.`;
    connectionProblem(message,true);
  };
  ws.onmessage=async({data})=>{
    if(data instanceof ArrayBuffer){
      if(!current||data.byteLength<18)return;const bytes=new Uint8Array(data),id=Array.from(bytes.slice(0,16),v=>v.toString(16).padStart(2,'0')).join('');if(id!==current.token.turn_id)return;
      const view=new DataView(data),samples=new Float32Array((data.byteLength-16)/2);for(let i=0;i<samples.length;i++)samples[i]=view.getInt16(16+i*2,true)/32768;
      avatar?.pcm(samples);
      node.port.postMessage({kind:'pcm',samples},[samples.buffer]);diagnostics.pcmPackets++;return;
    }
    const value=JSON.parse(data);
    if(value.kind==='ready'){
      clearTimeout(deadline);
      cfg=value.config;$('end-mode').value=cfg.end_mode;$('input-source').value=cfg.default_input;$('language').value=cfg.input_language;$('mode').textContent=cfg.mock?'Offline console':'Voice console';
      $('end-mode').querySelector('[value="handsfree"]').disabled=!cfg.handsfree;
      $('input-source').querySelector('[value="dell"]').disabled=!cfg.dell_mic_available;
      if(!avatar)import('/talk-avatar.js').then(module=>module.startAvatar($('talk-avatar'),()=>diagnostics.expression,cfg.avatar_available)).then(view=>{avatar=view;}).catch(()=>{});
      $('notice').textContent=cfg.mock?'Offline simulation. Speech recognition is simulated and replies use test tones.':'Speech recognition uses the configured provider. Replies are in English.';
      $('limit').textContent=`Up to ${cfg.max_seconds} seconds per input. Stop ends recording and playback.`;busy=false;controls();status('Ready');return;
    }
    if(value.input_id&&value.input_id!==inputId)return;
    if(value.kind==='reaction_reset'){reactionLocked=true;expression();return;}
    if(value.kind==='reaction'){
      if(!busy||!inputId||reactionLocked)return;
      const rank=value.phase==='partial'?1:value.phase==='final'?2:0;
      if(!rank||value.reaction_sequence!==rank||rank<=reactionSeq)return;
      if(value.token&&value.token.turn_id!==current?.token.turn_id)return;
      if(value.phase==='final'&&(!current||current.token.turn_id!==value.token?.turn_id))return;
      const previous=diagnostics.expression;reactionSeq=rank;
      if(previous!==value.reaction)expression(value.reaction);
      diagnostics.reactions.push({reaction:value.reaction,phase:value.phase,input_id:inputId,reaction_sequence:rank,
        transition:previous===value.reaction?'continue':value.reaction==='neutral'?'return_neutral':'switch',
        elapsed_ms:performance.now()-startAt,while_listening:listening&&inputEnd===null,before_turn:current===null});return;
    }
    if(value.kind==='armed'||value.kind==='listening'){
      if(!held||!inputId)return;
      if(mic&&!mic.connected){
        const recording=mic;
        recording.connected=true;recording.source.connect(recording.node);recording.node.connect(recording.ctx.destination);await recording.ctx.resume();
        if(!held||mic!==recording)return;
      }else{
        // Recording can open in parallel with the first worklet download over SSH.
        try{await outputReady;}catch{return;}
        if(!held||inputId!==value.input_id)return;
      }
      if(value.kind==='armed'){status(armedNotice);return;}
      listening=true;controls();
      $('talk').classList.add('recording');status('Listening…');timer=setTimeout(end,cfg.max_seconds*1000-200);return;
    }
    if(value.kind==='capture_ended'){
      if(held){
        inputEnd=performance.now();status('Waiting for reply…');
        const stopped=stopMic();controls();await stopped;
      }
      return;
    }
    if(value.kind==='turn'){
      discard();avatar?.reset();current={token:value.token,input_id:inputId,complete:false,ended:false};message('user',value.text);assistant=message('assistant','');
      node.port.postMessage({kind:'reset',turn_id:value.token.turn_id,jitter_ms:cfg.jitter_buffer_ms});meter.port.postMessage({turn_id:value.token.turn_id,not_before:ctx.currentTime});return;
    }
    if(value.kind==='text'&&current?.token.turn_id===value.token.turn_id){assistant.textContent+=value.text;$('messages').scrollTop=$('messages').scrollHeight;status('Replying…');}
    if(value.kind==='audio_end'&&current?.token.turn_id===value.token.turn_id){avatar?.finish();current.expectedFrames=value.frames;node.port.postMessage({kind:'finish'});}
    if(value.kind==='complete'&&current?.token.turn_id===value.token.turn_id){current.complete=true;current.metrics=value.metrics;ack();}
    if(value.kind==='played_ack'&&current?.token.turn_id===value.token.turn_id){
      if(!value.accepted){await fail('Playback completion could not be confirmed.');return;}
      diagnostics.playedAcks++;diagnostics.turns.push({...current});current=null;busy=false;controls();status('Ready');
      if(handsfree()&&!document.hidden)begin();
    }
    if(value.input_id && (value.kind==='error'||value.kind==='cancelled') &&
       ['STT_NO_FINAL_TRANSCRIPT','STT_TRANSCRIPT_TOO_LONG','NO_SPEECH','EMPTY_OR_OVERSIZED_STT'].includes(value.code)){
      // The server has finished this input. A fresh onset gets its own ID and STT reservation.
      const notice=value.code==='STT_TRANSCRIPT_TOO_LONG'?'The utterance was too long. ':'No transcript was available. ';
      const version=++epoch;
      inputId=null;preparing=false;reactionLocked=true;expression();discard();
      await stopMic();
      if(version!==epoch)return;
      busy=false;controls();
      if(handsfree()&&!document.hidden)await begin(notice+'Waiting for the next utterance…');
      else status(notice+'Try speaking again.');
      return;
    }
    if(value.kind==='error'){await fail(`Stopped: ${value.code}`);}
    if(value.kind==='cancelled'){leaveHandsfree();discard();await stopMic();busy=false;controls();status(value.code==='DELL_MIC_UNAVAILABLE'?'The host microphone is unavailable. Check the configured capture device.':'Stopped');}
  };
}
connect().catch(()=>connectionProblem('The console is unavailable. Check the local server or SSH connection, then reconnect.',true));
