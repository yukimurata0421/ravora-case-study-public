import {createPngtuberSurface, loadPngtuberImages} from '/pngtuber-surface.mjs';
import {createExpressionMotion, composeExpression, expressionName} from '/expressions.mjs';
import {buildPresenceTimeline, samplePresence, presenceForSurface, createIdleGlance} from '/presence.mjs';
import {createPcmMouth, speechEnvelope} from '/articulation.mjs';

// The artwork is registered to the idle sprite. Cut out the animated eyes during a
// blink and the articulated lower face during speech. Cache at patch resolution.
function spritePatch(bitmap, offset, regions, blink, speaking) {
  if (!blink && !speaking) return bitmap;
  if (!regions) return null;
  const patch=document.createElement('canvas');
  patch.width=bitmap.width;patch.height=bitmap.height;
  const g=patch.getContext('2d');
  g.drawImage(bitmap,0,0);
  g.globalCompositeOperation='destination-out';
  if (blink) {
    for (const eye of regions.eyes) {
      g.save();
      g.translate(eye.cx-offset[0],eye.cy-offset[1]);g.scale(eye.rx,eye.ry);
      const mask=g.createRadialGradient(0,0,0,0,0,1.15);
      mask.addColorStop(0.85,'black');mask.addColorStop(1,'transparent');
      g.fillStyle=mask;g.fillRect(-1.15,-1.15,2.3,2.3);g.restore();
    }
  }
  if (speaking) {
    const {cy,ry}=regions.mouth, top=cy-ry-offset[1];
    const mask=g.createLinearGradient(0,top,0,top+ry*.35);
    mask.addColorStop(0,'transparent');mask.addColorStop(1,'black');
    g.fillStyle=mask;g.fillRect(0,0,patch.width,patch.height);
  }
  return patch;
}

export async function startAvatar(canvas, reaction, hasArtwork) {
  const stage=canvas.parentElement, g=canvas.getContext('2d');
  if (!g) throw new Error('CANVAS_2D_UNAVAILABLE');
  const asset=hasArtwork?await (await fetch('/demo-avatar')).json():null;
  const sprites={...asset?.images}, names=new Set();
  for(const name of ['happy','surprise','thinking','concerned']){
    if(asset?.expressions?.[name]){
      sprites['expr_'+name]=asset.expressions[name];names.add(name);
    }
  }
  const images=asset?await loadPngtuberImages({images:sprites}):null;
  const patches=new Map(), spriteActing=createExpressionMotion(.22,names,null,false);
  const plan=buildPresenceTimeline([],180,20261116), glance=createIdleGlance(20261116);
  let surface, acting, mouth, clock, origin, last=-Infinity, weight=0;
  const idle={open:0,width:1,shape:'closed',db:-100,vowels:null};
  stage.dataset.renderer='sprites';
  function draw(now){
    requestAnimationFrame(draw);
    if(document.hidden||now-last<1000/30-1)return;
    const dt=Math.min(.1,(now-last)/1000);last=now;
    const t=now/1000, elapsed=clock?clock.currentTime-origin:null;
    const playing=elapsed!==null&&elapsed>=0&&Boolean(mouth);
    const lip=playing?mouth.sample(elapsed):{...idle};
    const movement=samplePresence(t%180,plan,.75,Math.max(0,Math.min(1,(lip.db+40)/25)));
    lip.blink=movement.blink;
    if(surface){
      const emotion=acting.sample(t,expressionName(reaction()),.65);
      const face=composeExpression(lip,emotion,playing);
      const target=playing?speechEnvelope(mouth.span(elapsed),elapsed):0;
      weight=Math.max(weight-dt/.4,Math.min(weight+dt/.12,target));
      face.expression.restPose=face.expression.mouthArt=1-weight;
      movement.idleGlance=glance(t,!playing);
      surface.render(face.open,face.blink,face.width,face.expression,presenceForSurface(movement,face.expression.tilt),face.vowels);
    }else if(!images){
      // A synthetic face makes the playback clock visible without redistributing avatar artwork.
      g.clearRect(0,0,canvas.width,canvas.height);
      g.save();g.translate(canvas.width/2+movement.bodyX,canvas.height*.46+movement.breath);
      g.fillStyle='#f7debd';g.beginPath();g.ellipse(0,0,260,225,0,0,Math.PI*2);g.fill();
      g.fillStyle='#303342';
      for(const x of [-91,91]){
        g.beginPath();g.ellipse(x,-28,18,Math.max(3,(reaction()==='surprise'?31:21)*(1-lip.blink)),0,0,Math.PI*2);g.fill();
      }
      g.beginPath();
      if(lip.open>.1){g.ellipse(0,87,47,9+lip.open*45,0,0,Math.PI*2);g.fill();}
      else{g.lineWidth=8;g.strokeStyle=g.fillStyle;g.moveTo(-40,78);g.quadraticCurveTo(0,reaction()==='joy'?119:91,40,78);g.stroke();}
      g.restore();
    }else{
      const blink=lip.blink>.4, talk=lip.open>.10;
      const bitmap=images[talk?(blink?'talking_blink':'talking'):(blink?'blink':'idle')];
      g.clearRect(0,0,canvas.width,canvas.height);
      const h=canvas.height,w=h*bitmap.width/bitmap.height;
      const x=(canvas.width-w)/2+movement.bodyX,y=movement.breath,scale=h/bitmap.height;
      g.drawImage(bitmap,x,y,w,h);
      const emotion=spriteActing.sample(t,expressionName(reaction()),1);
      for(const name of names){
        const amount=emotion['art_'+name];
        if(amount<=0)continue;
        const key=name+'-'+blink+'-'+playing, offset=asset.expressions[name].offset;
        if(!patches.has(key))patches.set(key,spritePatch(images['expr_'+name],offset,asset.regions,blink,playing));
        const patch=patches.get(key);
        if(patch){
          g.globalAlpha=amount;
          g.drawImage(patch,x+offset[0]*scale,y+offset[1]*scale,patch.width*scale,patch.height*scale);
        }
      }
      g.globalAlpha=1;
    }
    const mouthState=lip.open>.1?'open':'closed';
    if(stage.dataset.mouth!==mouthState)stage.dataset.mouth=mouthState;
  }
  requestAnimationFrame(draw);
  const probe=asset&&document.createElement('canvas').getContext('webgl2');
  let accelerated=false;
  if(probe){
    const info=probe.getExtension('WEBGL_debug_renderer_info');
    const renderer=probe.getParameter(info?info.UNMASKED_RENDERER_WEBGL:probe.RENDERER);
    accelerated=!/swiftshader|llvmpipe|software (rasterizer|renderer)|basic render driver/i.test(renderer);
    probe.getExtension('WEBGL_lose_context')?.loseContext();
  }
  if(accelerated){
    loadPngtuberImages(asset,images).then(()=>{
      const rig=createPngtuberSurface(images,asset);
      rig.resize(1080);
      acting=createExpressionMotion(.4,rig.expressionNames,20261116,false);
      // Display WebGL directly so presenting a frame needs no GPU-to-2D readback.
      rig.canvas.className='avatar-rig';rig.canvas.setAttribute('aria-hidden','true');
      canvas.after(rig.canvas);
      rig.canvas.addEventListener('webglcontextlost',()=>{
        surface=null;rig.canvas.remove();stage.dataset.renderer='sprites';
      },{once:true});
      surface=rig;stage.dataset.renderer='rig';
    }).catch(()=>{}); // Keep the animated poses when the GPU or textures are unavailable.
  }
  return {
    reset(){mouth=createPcmMouth(24000);clock=null;},
    pcm(samples){mouth?.push(samples);},
    start(context,frame){clock=context;origin=frame/context.sampleRate;},
    finish(){mouth?.finish();},
    stop(){clock=null;mouth=null;}
  };
}
