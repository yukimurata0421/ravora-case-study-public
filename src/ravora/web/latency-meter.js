// Observe the first non-silent 10 ms window on the actual Web Audio render thread.
// This tap outputs silence; the existing voice-to-recording connection is unchanged.
class RavoraLatencyMeter extends AudioWorkletProcessor{
  constructor(){
    super();this.arm=null;this.sum=0;this.count=0;this.windowStart=0;
    this.windowFrames=Math.max(1,Math.round(sampleRate*.01));
    this.port.onmessage=({data})=>{this.arm=data?.turn_id?data:null;this.sum=0;this.count=0;};
  }
  process(inputs){
    const samples=inputs[0]?.[0];if(!this.arm||!samples)return true;
    const notBefore=Math.ceil(this.arm.not_before*sampleRate);
    for(let i=0;i<samples.length;i++){
      const frame=currentFrame+i;if(frame<notBefore)continue;
      if(!this.count)this.windowStart=frame;
      this.sum+=samples[i]*samples[i];this.count++;
      if(this.count===this.windowFrames){
        const power=this.sum/this.count;
        if(power>=10**(-48/10)){
          this.port.postMessage({turn_id:this.arm.turn_id,audio_context_time:this.windowStart/sampleRate,
            detected_at_context_time:(frame+1)/sampleRate,rms_db:10*Math.log10(power),window_ms:this.windowFrames/sampleRate*1000,threshold_db:-48});
          this.arm=null;break;
        }
        this.sum=0;this.count=0;
      }
    }
    return true;
  }
}
registerProcessor('ravora-latency-meter',RavoraLatencyMeter);
