// Expression is a visual layer. It never schedules or replays speech.
// Procedural presets deform the original textures. When the avatar provides drawn expression
// artwork (asset.expressions, see scripts/build_expressions.py) for a preset, the artwork is
// cross-faded in instead (`art_<name>` weight) and the procedural deformation for it is off.
const EXPRESSION_ART=['smile','happy','surprise','thinking','shy','sad','concerned','sleepy','wink'];
const NO_ART=Object.fromEntries(EXPRESSION_ART.map(n=>['art_'+n,0]));
const EXPRESSION_PRESETS={
  neutral:{smile:0,browRaise:0,browTilt:0,squint:0,wide:0,restOpen:0,restWidth:1,tilt:0,gazeX:0,gazeY:0,winkA:0},
  smile:{smile:.6,browRaise:.12,browTilt:0,squint:.06,wide:0,restOpen:0,restWidth:1.03,tilt:-.012,gazeX:0,gazeY:0,winkA:0},
  happy:{smile:1,browRaise:.10,browTilt:0,squint:.12,wide:0,restOpen:.40,restWidth:1.05,tilt:-.025,gazeX:0,gazeY:0,winkA:0},
  surprise:{smile:0,browRaise:1,browTilt:0,squint:0,wide:1,restOpen:.28,restWidth:.84,tilt:.014,gazeX:0,gazeY:0,winkA:0},
  // Thinking glances up to the side; the eyes are moved by the gaze rig, not drawn sideways.
  thinking:{smile:0,browRaise:.25,browTilt:.5,squint:.08,wide:0,restOpen:0,restWidth:.97,tilt:.02,gazeX:3.4,gazeY:-1.5,winkA:0},
  shy:{smile:.35,browRaise:.05,browTilt:.25,squint:.2,wide:0,restOpen:0,restWidth:.98,tilt:-.03,gazeX:0,gazeY:0,winkA:0},
  sad:{smile:-.45,browRaise:.15,browTilt:.8,squint:.22,wide:0,restOpen:0,restWidth:.97,tilt:.02,gazeX:0,gazeY:0,winkA:0},
  concerned:{smile:-.55,browRaise:.1,browTilt:1,squint:.10,wide:0,restOpen:0,restWidth:.98,tilt:.025,gazeX:0,gazeY:0,winkA:0},
  sleepy:{smile:0,browRaise:-.4,browTilt:.15,squint:.62,wide:0,restOpen:.03,restWidth:1,tilt:.035,gazeX:0,gazeY:0,winkA:0},
  // Wink: the closing eye is driven by the eyelid rig (winkA), not the drawn closed eye: an open and a
  // closed eye cannot be morphed into each other and would show two eyes on top of each other.
  wink:{smile:.55,browRaise:.05,browTilt:0,squint:.1,wide:0,restOpen:0,restWidth:1.03,tilt:-.03,gazeX:0,gazeY:0,winkA:1}
};
// Reaction names that may arrive from the conversation JSON.
const EXPRESSION_ALIASES={joy:'happy',glad:'happy',surprised:'surprise',soft_smile:'smile',smiling:'smile',thoughtful:'thinking',
  embarrassed:'shy',bashful:'shy',sorrow:'sad',worried:'concerned',troubled:'concerned',tired:'sleepy',drowsy:'sleepy'};
export function expressionName(name){
  const key=typeof name==='string'?name.trim().toLowerCase():'';
  const resolved=EXPRESSION_ALIASES[key]||key;
  return resolved in EXPRESSION_PRESETS?resolved:'neutral';
}
// Strength 0..1. Drawn faces are morphed along their flow field, so a partial weight is a real,
// milder version of the expression (not a half-transparent second face).
export function expressionPreset(name='neutral',strength=1,art=null){
  const neutral={...EXPRESSION_PRESETS.neutral,...NO_ART};
  const amount=Number.isFinite(strength)?Math.min(1,Math.max(0,strength)):0;
  let selected={...(EXPRESSION_PRESETS[name]||EXPRESSION_PRESETS.neutral),...NO_ART};
  if(art&&typeof art.has==='function'&&art.has(name))selected={...neutral,tilt:selected.tilt,gazeX:selected.gazeX,gazeY:selected.gazeY,winkA:selected.winkA,['art_'+name]:1};
  return Object.fromEntries(Object.keys(neutral).map(k=>[k,neutral[k]+(selected[k]-neutral[k])*amount]));
}
// seed (optional): human variation. Each new expression gets its own intensity (x0.82..1.12) and the
// held face drifts slowly (+-6%), so the same cue never looks stamped out.
// Timing (seconds): an expression is never held at full strength. It swells from the calm face to its
// peak (onset, 0 -> 50 -> 100%), stays there only a moment (hold, random in range) and fades back
// to the calm face (release, random in range, 100 -> 50 -> 0%). A new cue (another name, or a new
// `cue` key such as the next utterance) brings it back.
// Changing directly from one expression to another takes `duration`.
// The pose is a pure function of time between cues, so pause/seek/export stay consistent.
export const EXPRESSION_TIMING={onset:.7,hold:[0,.25],release:[1.6,2.6]};
export function createExpressionMotion(duration=.28,art=null,seed=null,timing=null){
  const T=timing===false?null:{...EXPRESSION_TIMING,...(timing||{})};
  let from=expressionPreset(),to=expressionPreset(),started=-Infinity,name='neutral',strength=1,changes=0,cueKey=null,span=duration,hold=Infinity,release=1,fromCalm=true;
  const pick=(range,k)=>Array.isArray(range)?range[0]+(range[1]-range[0])*(seed===null?.5:hash(k)):range;
  const hash=n=>{let x=Math.imul((n|0)^Math.imul((seed|0)||1,0x9E3779B1),0x27d4eb2d);x^=x>>>15;x=Math.imul(x,0x85ebca6b);x^=x>>>13;return(x>>>0)/4294967296;};
  const noise=t=>{const i=Math.floor(t),f=t-i,u=f*f*(3-2*f);return(hash(i+7919)*(1-u)+hash(i+7920)*u)*2-1;};
  const neutral=expressionPreset();
  // Drawn faces are morphed, not cross-faded. Between two expressions the outgoing one relaxes
  // towards neutral over the first 60% and the incoming one forms over the last 60%; in the overlap
  // both are close to neutral, so the face never stops dead on neutral and two faces never blend.
  const ease=x=>{const t=Math.max(0,Math.min(1,x));return t*t*(3-2*t);};
  const deviation=pose=>Object.keys(pose).reduce((m,k)=>Math.max(m,Math.abs(pose[k]-neutral[k])),0);
  const value=time=>{
    const t=Math.max(0,Math.min(1,(time-started)/span));const drift=seed===null?1:1+.06*noise(time*.3);
    // Release: after the hold the whole expression eases back to the calm face.
    const r=T&&name!=='neutral'?1-ease((time-started-span-hold)/release):1;
    return Object.fromEntries(Object.keys(to).map(k=>{
      const u=fromCalm?ease(t):!k.startsWith('art_')||to[k]===from[k]?ease(t):to[k]>from[k]?ease((t-.4)/.6):ease(t/.6);
      let v=from[k]+(to[k]-from[k])*u;
      v=neutral[k]+(v-neutral[k])*r;
      return[k,k==='restWidth'?v:k.startsWith('art_')?Math.min(1,v*drift):v*drift];}));};
  return{
    // cue: optional key; when it changes, the same expression is raised again (e.g. per utterance).
    sample(time,nextName='neutral',nextStrength=1,cue=null){
      if(!(nextName in EXPRESSION_PRESETS))nextName='neutral';
      nextStrength=Number.isFinite(nextStrength)?Math.max(0,Math.min(1,nextStrength)):0;
      if(nextName!==name||nextStrength!==strength||(cue!==null&&cue!==cueKey&&nextName!=='neutral')){
        from=value(time);changes++;
        fromCalm=deviation(from)<.08;
        hold=T?pick(T.hold,changes+5000):Infinity;release=T?pick(T.release,changes+9000):1;
        span=T&&fromCalm&&nextName!=='neutral'?T.onset:T&&nextName==='neutral'?Math.max(duration,release*.6):duration;
        const vary=seed===null||nextName==='neutral'?1:.82+.3*hash(changes);
        to=expressionPreset(nextName,Math.min(1,nextStrength*vary),art);started=time;name=nextName;strength=nextStrength;
      }
      cueKey=cue;
      return value(time);
    },
    // Still changing (rising, changing or relaxing); false once settled.
    isMoving:time=>time<started+span+(T&&name!=='neutral'?hold+release:0),
    reset(){from=expressionPreset();to=expressionPreset();started=-Infinity;name='neutral';strength=1;changes=0;cueKey=null;span=duration;hold=Infinity;release=1;fromCalm=true;}
  };
}
export function composeExpression(lip,expression,speechActive=false,manualMouth=false,restWeight=1){
  // Preserve phoneme articulation and closed consonants; rest-mouth poses are only for listening.
  const keepMouth=speechActive||manualMouth;
  const rest=Math.max(0,Math.min(1,restWeight));
  return {...lip,
    open:keepMouth?lip.open:expression.restOpen*rest,
    vowels:keepMouth?lip.vowels:null,
    width:keepMouth?lip.width:1+(expression.restWidth-1)*rest,
    blink:lip.blink+(1-lip.blink)*expression.squint,
    tilt:(lip.tilt||0)+expression.tilt,
    // Drawn expression mouths are listening poses too: the articulated mouth wins while talking.
    expression:{...expression,mouthArt:keepMouth?0:rest}
  };
}

// Blend landmark poses on the playback clock. Packet arrival and gaps between
// words cannot restart the transition. A seek discards the previous segment.
export function createMouthHandoff() {
  let lastTime = -Infinity, start = 0, from = 1, target = 1;
  function valueAt(time) {
    const duration = target === 0 ? .23 : .42;
    const t = Math.min(1, Math.max(0, (time - start) / duration));
    return from + (target - from) * t * t * (3 - 2 * t);
  }
  return (time, speaking) => {
    const next = speaking ? 0 : 1;
    if (time < lastTime) { from = next; target = next; start = time; }
    if (next !== target) { from = valueAt(time); start = time; target = next; }
    lastTime = time;
    return valueAt(time);
  };
}
