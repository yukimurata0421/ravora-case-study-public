// Local review and finite clock probes. No provider calls.
export function evaluationUI(send, key) {
  let enabled=false, sequence=0, best=null, offset=null;
  const pending=new Map(), urls=new Set();
  const panel=document.getElementById('evaluation'), summary=document.getElementById('evaluation-summary');
  const rows=document.getElementById('evaluation-rows'), review=document.getElementById('evaluation-review');
  const text=(tag,value)=>{const node=document.createElement(tag);node.textContent=value;return node;};
  function request(){if(enabled)send({kind:'evaluation_snapshot'});}
  review.addEventListener('toggle',()=>{if(review.open)request();});
  document.getElementById('evaluation-refresh').addEventListener('click',request);
  function probe(){pending.set(sequence,performance.now());send({kind:'evaluation_sync',sequence});}
  function sync(){if(!enabled||pending.size)return;sequence=0;best=null;offset=null;probe();}
  const duration=value=>value===null?'not measured':(value/1000).toFixed(3)+' s';
  const rate=value=>value===null?'unreviewed':(value*100).toFixed(1)+'%';
  function show(data){
    const s=data.summary;
    summary.replaceChildren(text('p',`Collected ${s.played}/${s.config.max_turns} turns from ${s.inputs} inputs; target ${s.config.min_turns}${s.target_reached?' reached':''}${s.closed?'; closed':''}${s.limit?'; collection limit reached':''}`));
    for(const c of s.cohorts){
      const metric=c.latency.input_end_to_audio_ms, speech=c.latency.last_voice_to_audio_ms;
      summary.append(text('p',`${c.condition.input_source} / ${c.condition.end_mode} / ${c.condition.language} / ${c.condition.provenance}: input end to reply audio p50 ${duration(metric.p50)}, p95 ${duration(metric.p95)} (${metric.n} samples, ${metric.missing} missing). Estimated last voice to audio: p50 ${duration(speech.p50)}, p95 ${duration(speech.p95)}. STT reviewed ${c.stt_error.reviewed}/${c.inputs}, errors ${c.stt_error.errors}. Endpoints reviewed ${c.false_endpoint.reviewed}/${c.inputs}, errors ${c.false_endpoint.errors}.`));
      summary.append(text('p',`Reviewed transcription error rate ${rate(c.stt_error.rate)}; false endpoint rate ${rate(c.false_endpoint.rate)}. From ${c.transcription.references} reference texts: CER ${rate(c.transcription.cer)}, WER ${rate(c.transcription.wer)}.`));
    }
    if(s.errors.length)summary.append(text('p','Evaluation storage error: '+s.errors.join(', ')));
    if(!data.rows)return;
    for(const url of urls)URL.revokeObjectURL(url);urls.clear();rows.replaceChildren();
    for(const row of data.rows){
      const item=document.createElement('details');item.append(text('summary',`${row.created_at_jst} · ${row.outcome} · ${row.transcript??'no transcript'}`));
      item.append(text('p',`Input ID: ${row.id} / End reason: ${row.end_reason??'not ended'}`));
      if(row.audio_file){
        const play=text('button','Load input audio');play.type='button';
        play.addEventListener('click',async()=>{
          play.disabled=true;
          try{
            const r=await fetch('/evaluation/audio/'+row.id+'.wav',{headers:{Authorization:'Bearer '+key},signal:AbortSignal.timeout(10000)});
            if(!r.ok)throw Error();
            const url=URL.createObjectURL(await r.blob());urls.add(url);
            const audio=document.createElement('audio');audio.controls=true;audio.src=url;play.replaceWith(audio);
          }catch{play.disabled=false;play.textContent='Audio could not be loaded. Retry';}
        });item.append(play);
      }
      item.append(text('p','Transcript: '+(row.transcript??'not available')),text('p','Reply: '+(row.reply??'not generated')));
      const form=document.createElement('form');form.className='evaluation-form';
      const reference=document.createElement('textarea');reference.maxLength=1000;reference.value=row.reference??'';
      const note=document.createElement('textarea');note.maxLength=2000;note.value=row.note;
      const selections={};
      for(const [name,label] of [['stt_error','STT transcription error'],['false_endpoint','False endpoint (for example, premature submission)']]){
        const select=document.createElement('select');
        for(const [value,title] of [['unknown','unreviewed'],['no','no error'],['yes','error']]){const option=text('option',title);option.value=value;select.append(option);}
        select.value=row[name]===null?'unknown':row[name]?'yes':'no';selections[name]=select;
        const field=text('label',label+' ');field.append(select);form.append(field);
      }
      const refLabel=text('label','Reviewed reference text');refLabel.append(reference);form.append(refLabel);
      const noteLabel=text('label','Review note');noteLabel.append(note);form.append(noteLabel);
      const button=text('button','Save review');button.type='submit';form.append(button);
      form.addEventListener('submit',event=>{
        event.preventDefault();
        send({kind:'evaluation_annotate',id:row.id,fields:{reference:reference.value.trim()||null,note:note.value,
          stt_error:{unknown:null,no:false,yes:true}[selections.stt_error.value],false_endpoint:{unknown:null,no:false,yes:true}[selections.false_endpoint.value]}});
      });
      item.append(form);rows.append(item);
    }
  }
  return {
    ready(value){enabled=value;panel.hidden=!value;document.getElementById('recording-policy').textContent=value?'Input audio, transcripts, replies and timings are saved locally for evaluation.':'Input audio is not retained.';if(value){request();sync();}},
    sync,
    timing(current){
      if(!enabled||!current||current.evaluationSent)return;
      current.evaluationSent=true;
      const signal=current.signalPerformanceMs;
      send({kind:'evaluation_timing',token:current.token,timing:{input_end_to_audio_ms:current.from_input_end_ms??null,
        stt_final_to_audio_ms:signal!==undefined&&current.finalSttPerformanceMs!==undefined?Math.max(0,signal-current.finalSttPerformanceMs):null,
        signal_server_ms:signal!==undefined&&offset!==null?signal+offset:null,sync_uncertainty_ms:best===null?null:best/2,
        underflow_frames:current.underflow_frames??null}});
    },
    handle(value){
      if(!value.kind.startsWith('evaluation_'))return false;
      if(value.kind==='evaluation_sync'){
        const sent=pending.get(value.sequence);if(sent===undefined)return true;pending.delete(value.sequence);
        const received=performance.now(),rtt=received-sent;
        if(best===null||rtt<best){best=rtt;offset=value.server_ms-(sent+received)/2;}
        if(++sequence<5)probe();
      }else if(value.kind==='evaluation_error')summary.append(text('p','Evaluation: '+value.code));
      else show(value);
      return true;
    }
  };
}
