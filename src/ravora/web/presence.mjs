// Bounded 2D acting: gaze leads the head; the torso has its own slower motion.
// Times are in the same playback clock as speech, so pause/seek never run a separate actor clock.
//
// Randomness: every random choice (gesture timing/direction/size, blink intervals, thinking
// glances, smooth wander) comes from a seeded generator and is fixed when the timeline is built.
// The pose is then a pure function of time, so pause/seek/export stay consistent, while a
// different seed (e.g. each page load) gives different, non-repeating acting.
const presenceClamp=(x,lo,hi)=>Math.max(lo,Math.min(hi,x));
const presenceEase=x=>{const t=presenceClamp(x,0,1);return t*t*t*(t*(t*6-15)+10);};

function presenceRandom(seed){
  let s=(seed>>>0)||1;
  return()=>{s=s+0x6D2B79F5|0;let t=Math.imul(s^s>>>15,1|s);t=t+Math.imul(t^t>>>7,61|t)^t;return((t^t>>>14)>>>0)/4294967296;};
}
// Smooth value noise in [-1,1]: random values at integer times, quintic interpolation.
function presenceNoise(seed,t){
  const hash=n=>{let x=Math.imul((n|0)^Math.imul(seed|0,0x9E3779B1),0x27d4eb2d);x^=x>>>15;x=Math.imul(x,0x85ebca6b);x^=x>>>13;return((x>>>0)/4294967296)*2-1;};
  const i=Math.floor(t),f=t-i,u=f*f*f*(f*(f*6-15)+10);
  return hash(i)*(1-u)+hash(i+1)*u;
}
function presenceWander(seed,t){return presenceNoise(seed,t)*.7+presenceNoise(seed+101,t*2.3)*.3;}

export function buildPresenceTimeline(scenes,duration=60,seed=1){
  const random=presenceRandom(seed),between=(a,b)=>a+(b-a)*random(),sign=()=>random()<.5?-1:1;
  // pose: conversation-state head pose for the motion keyframe fields (turn/tilt, -1..1;
  // turn + faces the viewer's right, where comments are read; tilt + leans the head top to the
  // viewer's left). Steps are eased one after another, so the head always settles back.
  const gaze=[{time:0,x:0,y:0}],nods=[],blinks=[],gestures=[],speech=[],pose=[{time:0,turn:0,tilt:0}];
  for(const scene of scenes){
    speech.push([scene.speech_start,scene.speech_end]);
    const gap=scene.speech_start-scene.start;
    gaze.push({time:scene.start+.06,x:5.4,y:.7});
    // Listening: a slight turn towards the comment and a small head tilt. A scene added only when
    // the reply starts (live, listen:false) has no listening window left: no pose jump into the past.
    if(scene.listen!==false)pose.push({time:scene.start+.08,turn:between(.45,.62),tilt:-between(.3,.45)});
    // Thinking before answering (when there is time): glance up and away, head eases half back.
    if(scene.listen!==false&&gap>1.05&&random()<.8){
      const at=scene.speech_start-Math.min(.62,gap*.45);
      gaze.push({time:at,x:-between(1.8,2.8),y:-between(1,1.5)});
      pose.push({time:at-.05,turn:-between(.08,.2),tilt:between(.06,.16)});
    }
    gaze.push({time:scene.speech_start-.18,x:0,y:0});
    // Speaking: back to the camera; gestures below add small sways; after speaking, settle.
    pose.push({time:scene.speech_start-.12,turn:0,tilt:0});
    pose.push({time:scene.speech_end+.2,turn:0,tilt:0});
    nods.push({time:scene.start+.18,duration:.68,weight:.80});
    const length=scene.speech_end-scene.speech_start;
    if(length>3&&random()<.75)nods.push({time:scene.speech_start+length*between(.45,.75),duration:between(.7,.95),weight:between(.35,.55)});
    // Sometimes a brief thinking glance while talking, then eye contact again.
    if(length>3&&random()<.55){
      const at=scene.speech_start+length*between(.25,.6),side=sign();
      gaze.push({time:at,x:side*between(2.4,3.8),y:-between(.5,1.2)});
      gaze.push({time:at+between(.5,.9),x:0,y:0});
    }
    gaze.push({time:scene.speech_end+.35,x:between(.8,2),y:between(.1,.6)});
    // Talking gestures: overlapping small head sways, tilts, dips and weight shifts at
    // irregular intervals for the whole utterance, not only at its start.
    let side=sign();
    for(let t=scene.speech_start+between(.6,1.1);t<scene.speech_end-.35;t+=between(.55,1.4)){
      if(random()<.7)side=-side;
      const shift=random()<.7;
      gestures.push({time:t,duration:between(.8,1.5),
        headX:side*between(.8,2.2),headTilt:sign()*between(.004,.009),headY:between(0,1.4),
        bodyX:shift?side*between(.6,1.8):0,bodyTilt:shift?side*between(.0015,.0035):0,
        turn:side*between(.05,.16),tilt:sign()*between(.04,.13)});
    }
  }
  // Occasional posture shifts while listening or idle, smaller and slower.
  for(let t=between(1.5,3);t<duration;t+=between(2.5,5)){
    if(speech.some(([a,b])=>t>a-.3&&t<b))continue;
    const side=sign();
    gestures.push({time:t,duration:between(1.4,2.2),headX:side*between(.4,1.2),headTilt:sign()*between(.002,.005),headY:between(0,.6),
      bodyX:side*between(.4,1.2),bodyTilt:side*between(.001,.0025),turn:side*between(.03,.08),tilt:sign()*between(.02,.07)});
  }
  gaze.sort((a,b)=>a.time-b.time);pose.sort((a,b)=>a.time-b.time);gestures.sort((a,b)=>a.time-b.time);
  // Irregular blinks, sometimes a quick double blink.
  for(let at=between(1.8,3.2);at<duration;at+=between(2.4,6)){
    blinks.push({time:at,duration:.24});
    if(random()<.12)blinks.push({time:at+.32,duration:.22});
  }
  return {gaze,nods,blinks,gestures,speech,pose,duration,seed};
}

function presencePose(t,events=[],transition=.8){
  let turn=0,tilt=0;
  for(const event of events){
    if(event.time>t)break;
    const mix=presenceEase((t-event.time)/transition);
    turn+=(event.turn-turn)*mix;tilt+=(event.tilt-tilt)*mix;
  }
  return {turn,tilt};
}

function presenceGaze(t,events,transition){
  let x=0,y=0;
  for(const event of events){
    if(event.time>t)break;
    const mix=presenceEase((t-event.time)/transition);
    x+=(event.x-x)*mix;y+=(event.y-y)*mix;
  }
  return {x,y};
}

// Speech emphasis 0..1 from a 10 ms loudness track: short-window loudness above the recent
// phrase average. Stateless in time, so pause/seek/export give identical motion.
export function prosodyAt(levels,hop,time){
  if(!Array.isArray(levels)||!(hop>0)||!Number.isFinite(time))return 0;
  const mean=(a,b)=>{let sum=0,n=0;for(let t=a;t<=b;t+=hop){const v=levels[Math.round(t/hop)];if(Number.isFinite(v)){sum+=Math.max(-60,v);n++;}}return n?sum/n:-60;};
  const smooth=x=>{const e=presenceClamp(x,0,1);return e*e*(3-2*e);};
  // Soft voice gate (no step at speech onset) and a 8-tap triangular average over ~0.25 s,
  // which keeps the per-frame head step at the same scale as the scripted nods.
  const raw=t=>{const now=mean(t-.22,t-.04),phrase=mean(t-1.2,t-.04);return smooth((now-phrase)/10)*smooth((now+50)/14);};
  let sum=0,weights=0;
  for(let k=0;k<8;k++){const w=1-Math.abs(k-3.5)/4;sum+=raw(time-k*.035)*w;weights+=w;}
  return sum/weights;
}

// 0 while silent, 1 while talking, with soft 0.3 s edges.
function presenceTalking(t,speech=[]){
  let w=0;
  for(const [a,b] of speech)w=Math.max(w,presenceEase((t-a+.15)/.3)*(1-presenceEase((t-b+.15)/.3)));
  return w;
}

export function samplePresence(time,plan,amount=.75,prosody=0){
  const t=Number.isFinite(time)?Math.max(0,time):0;
  const strength=Number.isFinite(amount)?presenceClamp(amount,0,1):0;
  const seed=plan.seed??1;
  const gaze=presenceGaze(t,plan.gaze,.16),head=presenceGaze(t-.13,plan.gaze,.64);
  let nod=0,blink=0;
  for(const event of plan.nods){const p=(t-event.time)/event.duration;if(p>0&&p<1)nod+=event.weight*Math.sin(Math.PI*p)**2;}
  for(const event of plan.blinks){const p=(t-event.time)/event.duration;if(p>=0&&p<=1)blink=Math.max(blink,p<.38?presenceEase(p/.38):p<.52?1:1-presenceEase((p-.52)/.48));}
  // Gestures: ease in (40%), short hold, ease out. Overlaps add up into continuous motion.
  const g={headX:0,headY:0,headTilt:0,bodyX:0,bodyTilt:0,turn:0,tilt:0};
  for(const event of plan.gestures||[]){
    if(event.time>t)break;
    const p=(t-event.time)/event.duration;if(p>=1)continue;
    const e=p<.4?presenceEase(p/.4):p<.55?1:1-presenceEase((p-.55)/.45);
    for(const k in g)g[k]+=(event[k]||0)*e;
  }
  const talk=presenceTalking(t,plan.speech);
  // Seeded smooth wander: larger while talking, never zero while listening.
  const wander=.35+.65*talk;
  const breath=Math.sin(t*2*Math.PI/4.9+.3)*.78+Math.sin(t*2*Math.PI/11.7)*.22;
  const bodyTilt=presenceClamp((Math.sin(t*.63)*.0035+Math.sin(t*.27+.5)*.0018+g.bodyTilt+presenceWander(seed+5,t*.45)*.0012*wander)*strength,-.0115,.0115);
  const emphasis=Number.isFinite(prosody)?presenceClamp(prosody,0,1):0;
  // Emphasis dips the head slightly and adds a small tilt whose side drifts slowly between phrases.
  const headTilt=presenceClamp((Math.sin(t*.79+.4)*.006+Math.sin(t*.33)*.003-head.x*.0003+emphasis*.006*Math.sin(t*.45+1.1)
    +g.headTilt+presenceWander(seed+1,t*.6)*.004*wander)*strength,-.028,.028);
  // Head pose fields: the conversation state (listening / thinking / speaking), gesture sways and a
  // slow seeded drift, so the head is never frozen in one pose.
  const state=presencePose(t,plan.pose,.8);
  const poseTurn=presenceClamp((state.turn+g.turn+presenceWander(seed+6,t*.32)*.05)*strength/.75,-.7,.7);
  const poseTilt=presenceClamp((state.tilt+g.tilt+presenceWander(seed+7,t*.27)*.04)*strength/.75,-.6,.6);
  return{
    poseTurn,poseTilt,
    gazeX:presenceClamp(gaze.x+Math.sin(t*1.37)*.30,-6,6)*strength,
    gazeY:presenceClamp(gaze.y+Math.sin(t*.93+.5)*.16,-3,3)*strength,
    headTilt,
    headX:presenceClamp((head.x*.2+Math.sin(t*.51)*.8+g.headX+presenceWander(seed+2,t*.55)*1.1*wander)*strength,-7.5,7.5),
    headY:presenceClamp((nod*5.6+emphasis*3.2+g.headY+Math.sin(t*.77)*.65+presenceWander(seed+3,t*.7)*.5*wander)*strength,-9.5,9.5),
    bodyTilt,
    bodyX:presenceClamp((Math.sin(t*.38+.2)*1.2+g.bodyX+presenceWander(seed+4,t*.4)*.6*wander)*strength,-3.8,3.8),
    breath:breath*2.4*strength,
    blink:strength>0?blink:0,nod:presenceClamp(nod,0,1),emphasis,talk,amount:strength
  };
}

export function presenceForSurface(movement,expressionTilt=0){
  return {...movement,headTilt:presenceClamp(movement.headTilt+expressionTilt,-.06,.06)};
}

// A glance belongs to one uninterrupted period of inactivity. When a turn
// begins, release from the current position even halfway through the motion.
export function createIdleGlance(seed = 1) {
  const random = presenceRandom(seed);
  let next = Infinity, quiet = false, last = -Infinity, weight = 0;
  let releaseAt = 0, releaseFrom = 0;
  return (time, idle) => {
    if (time < last) { quiet = false; weight = 0; releaseFrom = 0; }
    if (idle && !quiet) next = time + .85 + random() * .55;
    if (!idle && quiet) { releaseAt = time; releaseFrom = weight; }
    quiet = idle;
    last = time;
    const returning = releaseFrom * (1 - presenceEase((time - releaseAt) / .18));
    if (idle) {
      if (time > next + .92) next = time + 2.8 + random() * 3;
      const elapsed = time - next;
      const glance = presenceEase(elapsed / .19) * (1 - presenceEase((elapsed - .53) / .39));
      weight = Math.max(returning, glance);
    } else {
      weight = returning;
    }
    return weight;
  };
}
