// One native-resolution surface shared by the preview and the portable demo.
// Mesh vertices move linearly between registered landmarks. The source textures
// are warped to that shared geometry before blending.
const VOWELS = ['A', 'I', 'U', 'E', 'O'];

export function createPngtuberSurface(images, asset) {
  const base = images.idle, canvas = document.createElement('canvas');
  canvas.width = base.width; canvas.height = base.height;
  const gl = canvas.getContext('webgl2', { alpha: true, premultipliedAlpha: true, antialias: false, preserveDrawingBuffer: true });
  if (!gl) throw Error('PNGTUBER_WEBGL2_REQUIRED');
  const registeredMouthFragment = `
    if (kind == 7) {
      // The whole mesh is drawn: its border rows and columns are fixed, so the warped skin meets the
      // face without a seam and the chin and jaw move as one piece (a box clip around the lips and a
      // fade below the lower lip cut the lowered chin off and showed a mouth/jaw edge).
      float coverage = 1.;
      // The grid lands texels at fractional positions; bilinear resampling softened the lips
      // (closed thinking lips 1.76 against 2.08 unwarped). Catmull-Rom keeps them sharp.
      a = sampleSharp(tex0, uv0);
      // With an expression drawn, tex0 is the expression face. Near the lips that face is a feathered
      // mix (its own lips are hidden there and its grin creases half remain), so the neutral portrait
      // is used there and the expression face (smile cheeks, blush) from 1.6 lip radii outward.
      if (mouthFace > .5) {
        float far = smoothstep(1.2, 1.6, length((uv0 * size - region.xy) / (region.zw * vec2(.85, .75))));
        a = mix(sampleSharp(mouthNeutral, uv0), a, far);
      }
      vec4 donor;
      if (useMultiDonor > .5) {
        donor = vec4(0.);
        if (mouthWeights[0] > 0.) donor += sampleSharp(mouthVowels[0], mouthUV0.zw) * mouthWeights[0];
        if (mouthWeights[1] > 0.) donor += sampleSharp(mouthVowels[1], mouthUV1.xy) * mouthWeights[1];
        if (mouthWeights[2] > 0.) donor += sampleSharp(mouthVowels[2], mouthUV1.zw) * mouthWeights[2];
        if (mouthWeights[3] > 0.) donor += sampleSharp(mouthVowels[3], mouthUV2.xy) * mouthWeights[3];
        if (mouthWeights[4] > 0.) donor += sampleSharp(mouthVowels[4], mouthUV2.zw) * mouthWeights[4];
      } else {
        donor = sampleSharp(tex1, uv1);
      }
      // Drawn mouth art only on the lips and in the mouth (per-vertex lip weight of the grid, fading
      // within one refined cell outside the lip rows and corner columns). Cheeks, chin and jaw are
      // always the warped base skin: the vowel and expression crops draw their jaws elsewhere.
      float skin = smoothstep(.35, 1., vLip);
      // Head-fixed expression upper-teeth plate: anchored at the anatomical maxilla height across all
      // expression strengths (independent of vowel/opening so teeth stay still during speech). Rounded
      // vowels (U/O) attenuate the wide-smile tooth plate via cavityScale so pursed lips naturally shade
      // and cover the grin teeth instead of flashing wide teeth through a narrow keyhole.
      if (exprMix > 0. && inside > .95 && cavityScale > .001) {
        float inner = smoothstep(.95, 1., inside);
        vec2 here = (position - voiceCrop.xy) / max(voiceCrop.zw, vec2(1.));
        float py = 1. / max(1., voiceCrop.w);
        float e = clamp(exprMix, 0., 1.);
        float anchor = mix(restUV - 4. * py, topUV, clamp(.45 + .55 * e, 0., 1.));
        float below = here.y - anchor;
        vec3 oral = sampleColor(tex3, vec2(here.x, topUV + max(0., below))).rgb;
        if (below < 0.) {
          // Upper lip lifted slightly above tooth top: gentle upper-lip occlusion shadow and soft natural
          // gum hint only at extreme lift, never sampling the dark lip rim above topUV (brown stripe).
          float shade = mix(1., .86, smoothstep(0., 3.5 * py, -below));
          float gumMix = smoothstep(2.5 * py, 8. * py, -below) * .42;
          oral = mix(oral * shade, vec3(.74, .46, .45) * shade, gumMix);
        }
        donor.rgb = mix(donor.rgb, oral, smoothstep(0., .45, exprMix) * inner * smoothstep(0., 1., across) * cavityScale);
      }
      // Separate lip-surface transition from oral-cavity reveal so opening from 0 -> 1 px never snaps
      // the full stretched lip texture onto a barely parted mouth, and add soft lip occlusion inside
      // tiny openings (< 4.5 px).
      float cavMask = smoothstep(.35, .95, inside);
      float lipMat = smoothstep(.6, 8.5, amount);
      float cavMat = smoothstep(.35, 3.0, amount);
      float material = mix(lipMat, cavMat, cavMask) * donor.a * skin;
      vec3 donorRgb = donor.rgb * mix(1., mix(.74, 1., smoothstep(.8, 4.5, amount)), cavMask);
      vec3 rgb = mix(a.rgb, donorRgb, material);
      if (mouthPaint.x > 0. && skin > 0.) {
        // Older sets borrow fine lip detail from the portrait. A detailed registered
        // set carries its own lip lines and highlights, including on the closed shape.
        const float LOW = 1.5;
        vec4 closed = sampleSharp(tex2, mouthUV0.xy), closedLow = sampleColorLod(tex2, mouthUV0.xy, LOW);
        vec4 painted = closed, paintedLow = closedLow;
        if (mouthPaint.y > 0.) {
          vec4 voiced = vec4(0.), voicedLow = vec4(0.);
          if (mouthWeights[0] > 0.) { voiced += sampleSharp(mouthVowels[0], mouthUV0.zw) * mouthWeights[0]; voicedLow += sampleColorLod(mouthVowels[0], mouthUV0.zw, LOW) * mouthWeights[0]; }
          if (mouthWeights[1] > 0.) { voiced += sampleSharp(mouthVowels[1], mouthUV1.xy) * mouthWeights[1]; voicedLow += sampleColorLod(mouthVowels[1], mouthUV1.xy, LOW) * mouthWeights[1]; }
          if (mouthWeights[2] > 0.) { voiced += sampleSharp(mouthVowels[2], mouthUV1.zw) * mouthWeights[2]; voicedLow += sampleColorLod(mouthVowels[2], mouthUV1.zw, LOW) * mouthWeights[2]; }
          if (mouthWeights[3] > 0.) { voiced += sampleSharp(mouthVowels[3], mouthUV2.xy) * mouthWeights[3]; voicedLow += sampleColorLod(mouthVowels[3], mouthUV2.xy, LOW) * mouthWeights[3]; }
          if (mouthWeights[4] > 0.) { voiced += sampleSharp(mouthVowels[4], mouthUV2.zw) * mouthWeights[4]; voicedLow += sampleColorLod(mouthVowels[4], mouthUV2.zw, LOW) * mouthWeights[4]; }
          float paintBlend = mix(smoothstep(.015, .24, mouthPaint.y), smoothstep(.008, .12, mouthPaint.y), cavMask);
          painted = mix(closed, voiced, paintBlend);
          paintedLow = mix(closedLow, voicedLow, paintBlend);
        }
        if (${asset.expressions?.thinking?.authoredMouthDetail ? 'false' : 'true'}) {
          vec3 detail = sampleSharp(mouthNeutral, uv0).rgb - sampleColorLod(mouthNeutral, uv0, LOW).rgb;
          float lips = 1. - smoothstep(.3, .9, inside);
          painted.rgb = mix(painted.rgb, clamp(paintedLow.rgb + detail, 0., 1.), lips);
        } else {
          // Blending two registered textures dampens fine lines at partial expression strength.
          // Compensate only the lip surface, tapering to zero at either endpoint.
          float mixed = 4. * mouthPaint.x * (1. - mouthPaint.x);
          float lipSurface = 1. - smoothstep(.3, .9, inside);
          vec3 detail = clamp(.5 * (painted.rgb - paintedLow.rgb), vec3(-.03), vec3(.03));
          painted.rgb = clamp(painted.rgb + mixed * lipSurface * detail, 0., 1.);
        }
        rgb = mix(rgb, painted.rgb, mouthPaint.x * painted.a * skin);
      }
      color = vec4(rgb * a.a * coverage, a.a * coverage);
      return;
    }`;
  const vertex = '#version 300 es\nprecision highp float; precision highp int; in vec2 source; in vec4 mouthCoords0; in vec4 mouthCoords1; in vec4 mouthCoords2; out vec4 mouthUV0; out vec4 mouthUV1; out vec4 mouthUV2; in vec3 cavityTop; out float topUV; out float across; out float restUV; uniform vec4 voiceCrop; in vec2 target; in vec2 target2; in vec2 shape; uniform int shapeMode; in float lipw; out float vLip; uniform float exprMix; in float cavity; in float flex; uniform vec2 size; uniform float amount; uniform float mouthWidth; uniform vec4 mood; uniform vec3 headMotion; uniform vec3 bodyMotion; uniform float browSide; uniform int kind; uniform vec4 region; uniform vec2 extent; uniform vec4 crop; uniform float outFlip; uniform float flatAmount; out vec2 uv0; out vec2 uv1; out vec2 uv2; out vec2 position; out float inside; out float vAmount; uniform sampler2D motion0; uniform sampler2D motion1; uniform sampler2D motion2; uniform sampler2D motion3; uniform vec4 poseW; uniform vec4 gazeW; vec2 dec(vec2 v){return (v*255.-128.)/4.;} void main(){float am=amount;if(kind==1){float xn=min(1.,abs(source.x-region.x)/(region.z*.5));am=flatAmount>.5?max(amount,0.):pow(max(amount,0.),1.+(1.2+max(0.,1.-mouthWidth)*7.)*xn*xn);}vAmount=am;vLip=lipw;mouthUV0=mouthCoords0;mouthUV1=mouthCoords1;mouthUV2=mouthCoords2;topUV=(cavityTop.x-voiceCrop.y)/max(1.,voiceCrop.w);across=cavityTop.y;restUV=(cavityTop.z-voiceCrop.y)/max(1.,voiceCrop.w);if(kind==1&&flatAmount>.5)vAmount=am*(1.-smoothstep(.45,.8,abs(source.x-region.x)/(region.z*1.6)))*(1.-smoothstep(.6,1.,abs(source.y-region.y)/(region.w*(source.y<region.y?1.:1.6))));position=shapeMode==1?mix(source,shape,am):mix(source,mix(target,target2,exprMix),am);uv2=kind==7?(target2-voiceCrop.xy)/max(voiceCrop.zw,vec2(1.)):(target2-crop.xy)/crop.zw;if(kind==1){vec2 o=abs(source-region.xy);float rows=1.-smoothstep(region.w*.5,region.w*.95,o.y);float fade=1.-smoothstep(region.z*.35,region.z*.92,o.x);position.x+=(position.x-region.x)*(mouthWidth-1.)*fade*rows*flex;float corner=pow(min(1.,o.x/(region.z*.7)),1.6)*(1.-smoothstep(region.z*.95,extent.x*.95,o.x))*rows;position.y-=mood.x*8.*corner*flex;}if(kind==2){position.y+=(position.y-region.y)*.20*mood.w*(1.-amount)*flex;position.y-=max(0.,mood.x)*4.*(1.-amount)*smoothstep(-.1,.45,(source.y-region.y)/region.w)*flex;}if(kind==3)position.y-=flex*(mood.y*12.+mood.z*12.*browSide*(source.x-region.x)/region.z);uv0=source/size;if(kind==7)position=shape;uv1=(kind==1||kind==5||kind==7||kind==8)?(target-crop.xy)/crop.zw:target/size;inside=cavity;if(kind==6){vec2 m=source/size;vec4 a=textureLod(motion0,m,0.),b=textureLod(motion1,m,0.),c=textureLod(motion2,m,0.),e=textureLod(motion3,m,0.);position=source+dec(a.rg)*poseW.x+dec(a.ba)*poseW.y+dec(b.rg)*poseW.z+dec(b.ba)*poseW.w+dec(c.rg)*gazeW.x+dec(c.ba)*gazeW.y+dec(e.rg)*gazeW.z+dec(e.ba)*gazeW.w;}vec2 p=position;vec2 neck=size*vec2(.5,.59);float headWeight=1.-smoothstep(size.y*.52,size.y*.68,p.y);float hc=cos(headMotion.x),hs=sin(headMotion.x);vec2 d=p-neck;vec2 head=vec2(hc*d.x-hs*d.y,hs*d.x+hc*d.y)+neck+headMotion.yz;p=mix(p,head,headWeight);p.y+=bodyMotion.y*(1.-smoothstep(size.y*.65,size.y,p.y));vec2 root=size*vec2(.5,.96);d=p-root;float bc=cos(bodyMotion.x),bs=sin(bodyMotion.x);p=vec2(bc*d.x-bs*d.y,bs*d.x+bc*d.y)+root;p.x+=bodyMotion.z*(1.-smoothstep(size.y*.75,size.y,p.y));gl_Position=vec4(p.x/size.x*2.-1.,(1.-p.y/size.y*2.)*outFlip,0.,1.);}';
  const fragment = '#version 300 es\nprecision highp float; precision highp int; uniform sampler2D tex0; uniform sampler2D tex1; uniform float amount; uniform int kind; uniform vec4 region; uniform vec2 extent; uniform sampler2D tex2; uniform float layer; uniform float teethHide; uniform float fullArt; uniform vec2 size; uniform vec2 gaze; uniform vec4 eyeA; uniform vec4 eyeB; uniform vec2 fade; uniform vec4 crop; uniform float blend; uniform float faceBlink; uniform vec4 eyeExt; uniform vec2 eyeHide; uniform float lipHide; uniform vec2 detailBand; uniform float flatAmount; in vec2 uv0; in vec2 uv1; in vec2 uv2; in vec4 mouthUV0; in vec4 mouthUV1; in vec4 mouthUV2; uniform vec2 mouthPaint; uniform float useMultiDonor; uniform float cavityScale; uniform sampler2D mouthNeutral; uniform float mouthFace; uniform sampler2D mouthVowels[5]; uniform float mouthWeights[5]; in float topUV; in float across; in float restUV; uniform sampler2D tex3; uniform float exprMix; uniform vec4 voiceCrop; in float vLip; in vec2 position; in float inside; in float vAmount; out vec4 color; vec3 linearize(vec3 c){return mix(c/12.92,pow((c+.055)/1.055,vec3(2.4)),step(vec3(.04045),c));} vec3 encode(vec3 c){return mix(c*12.92,1.055*pow(max(c,vec3(0.)),vec3(1./2.4))-.055,step(vec3(.0031308),c));} vec4 sampleColorBias(sampler2D tex,vec2 uv,float bias){vec4 c=texture(tex,uv,bias);float a=c.a;vec3 rgb=a>.0001?c.rgb/a:vec3(0.);a=a>.98?1.:a*smoothstep(.008,.06,a);return vec4(rgb,a);} vec4 sampleColor(sampler2D tex,vec2 uv){return sampleColorBias(tex,uv,0.);} vec4 sampleSharp(sampler2D tex,vec2 uv){vec2 ts=vec2(textureSize(tex,0));vec2 sp=uv*ts,tc=floor(sp-.5)+.5,f=sp-tc;vec2 w0=f*(-.5+f*(1.-.5*f)),w1=1.+f*f*(-2.5+1.5*f),w2=f*(.5+f*(2.-1.5*f)),w3=f*f*(-.5+.5*f);vec2 w12=w1+w2,t0=(tc-1.)/ts,t3=(tc+2.)/ts,t12=(tc+w2/w12)/ts;vec4 c=(textureLod(tex,vec2(t0.x,t0.y),0.)*w0.x+textureLod(tex,vec2(t12.x,t0.y),0.)*w12.x+textureLod(tex,vec2(t3.x,t0.y),0.)*w3.x)*w0.y+(textureLod(tex,vec2(t0.x,t12.y),0.)*w0.x+textureLod(tex,vec2(t12.x,t12.y),0.)*w12.x+textureLod(tex,vec2(t3.x,t12.y),0.)*w3.x)*w12.y+(textureLod(tex,vec2(t0.x,t3.y),0.)*w0.x+textureLod(tex,vec2(t12.x,t3.y),0.)*w12.x+textureLod(tex,vec2(t3.x,t3.y),0.)*w3.x)*w3.y;c=clamp(c,0.,1.);float al=c.a;vec3 rgb=al>.0001?min(c.rgb/al,vec3(1.)):vec3(0.);al=al>.98?1.:al*smoothstep(.008,.06,al);return vec4(rgb,al);} vec4 sampleColorLod(sampler2D tex,vec2 uv,float lod){vec4 c=textureLod(tex,uv,lod);float a=c.a;vec3 rgb=a>.0001?c.rgb/a:vec3(0.);a=a>.98?1.:a*smoothstep(.008,.06,a);return vec4(rgb,a);} void main(){if(kind==6){color=texture(tex0,uv0);return;}if(kind==8){color=mix(texture(tex0,uv1),texture(tex1,uv1),amount)*layer;return;}vec2 eyeUV=uv0;if(kind==2){vec2 q=(uv0*size-region.xy)/vec2(region.z*.65,region.w*.82);float mask=(1.-smoothstep(.52,1.,length(q)))*max(inside,0.);eyeUV-=gaze*mask/size;}float band=max(-inside,0.),cav=max(inside,0.);vec4 a=sampleColor(tex0,eyeUV);if(kind==2&&band>0.){vec4 o=sampleColor(tex2,eyeUV);a=faceBlink>.5?mix(a,o,band*smoothstep(.04,.3,amount)):mix(o,a,band*smoothstep(.04,.3,amount));}if(kind==0){color=vec4(a.rgb*a.a,a.a);return;}if(kind==4){float h=a.a*(1.-smoothstep(.55,1.,amount))*(1.-smoothstep(.78,1.,length((position-region.xy)/region.zw)));color=vec4(a.rgb*h,h);return;}if(kind==5){vec2 p=uv0*size;vec2 qa=(p-eyeA.xy)/eyeA.zw;vec2 qb=(p-eyeB.xy)/eyeB.zw;float hideA=(1.-smoothstep(.75,.98,length((p-eyeA.xy)/eyeExt.xy)))*eyeHide.x;float hideB=(1.-smoothstep(.75,.98,length((p-eyeB.xy)/eyeExt.zw)))*eyeHide.y;float iris=max(1.-smoothstep(15.,24.,length(p-eyeA.xy-vec2(2.,-4.))),1.-smoothstep(15.,24.,length(p-eyeB.xy-vec2(-1.,-4.))));vec2 dm=(p-region.xy)/(region.zw*vec2(1.65,p.y<region.y?1.67:2.9));float mz=1.-smoothstep(1.,1.3,length(dm));float zone=mix(1.,fade.x,mz);vec4 fl=texture(tex2,uv1);vec2 F=(fl.rg*255.-128.)/4.;float t=layer*zone;float c=mix(smoothstep(0.,.03,t),smoothstep(.25,.5,t),1.-fl.a);vec2 g=vec2(clamp(gaze.x,-4.5,4.5),clamp(gaze.y,-1.2,1.2)*.4)*iris;vec4 n=sampleColor(tex0,(p-t*F-g)/size);vec2 pe=p+(1.-t)*F;vec4 e=sampleColor(tex1,(pe-crop.xy)/crop.zw);vec4 ne=sampleColor(tex0,pe/size);vec3 rgb=encode(mix(linearize(n.rgb),linearize(mix(ne.rgb,e.rgb,e.a)),c));if(detailBand.y>detailBand.x)rgb=mix(rgb,n.rgb,1.-smoothstep(detailBand.x,detailBand.y,p.y));float al=mix(n.a,ne.a,c)*fl.b*zone*(1.-hideA)*(1.-hideB)*(1.-lipHide*(1.-smoothstep(1.,1.35,length((p-region.xy)/(region.zw*vec2(.85,.75))))))*step(.001,layer)*blend;color=vec4(rgb*al,al);return;}' + registeredMouthFragment + 'vec4 b=sampleColor(tex1,uv1);if(kind==1&&exprMix>0.){b=mix(b,sampleColor(tex3,uv2),smoothstep(0.,.2,exprMix)*smoothstep(.8,1.,vLip));}float w=kind==1?vAmount:amount;if(kind==1){w=mix(w,smoothstep(0.,.10,vAmount),cav);w=mix(w,mix(smoothstep(0.,.15,vAmount),smoothstep(.12,.3,vAmount),1.-smoothstep(.25,.45,dot(b.rgb,vec3(.299,.587,.114)))),flatAmount);float lum=dot(b.rgb,vec3(.299,.587,.114));float teeth=smoothstep(.55,.72,lum)*(1.-smoothstep(.10,.18,b.r-b.b))*cav;b.rgb=mix(b.rgb,vec3(.17,.08,.08),teeth*teethHide*(1.-smoothstep(.3,.7,vAmount)));}if(kind==2)w=mix(smoothstep(.55,1.,amount),smoothstep(.88,1.,amount),cav);float d=length((position-region.xy)/region.zw);float mask=1.-smoothstep(.78,1.,d);if(kind==1){if(fullArt>.5){vec2 q=abs(position-region.xy)/extent;q*=q;q*=q;mask=1.-smoothstep(.86,.98,pow(q.x+q.y,.25));vec2 e=min(uv1,1.-uv1);w*=smoothstep(0.,.05,e.x)*smoothstep(0.,.05,e.y);}else{w*=mask;mask=1.-smoothstep(.80,.98,length((position-region.xy)/vec2(extent.x,region.w)));}}vec3 rgb=encode(mix(linearize(a.rgb),linearize(b.rgb),w));float alpha=mix(a.a,b.a,w)*mask*(kind==1?layer:1.);color=vec4(rgb*alpha,alpha);}';
  function shader(type, code) { const s = gl.createShader(type); gl.shaderSource(s, code); gl.compileShader(s); if (!gl.getShaderParameter(s, gl.COMPILE_STATUS)) throw Error(gl.getShaderInfoLog(s)); return s; }
  const program = gl.createProgram(), vs = shader(gl.VERTEX_SHADER, vertex), fs = shader(gl.FRAGMENT_SHADER, fragment);
  gl.attachShader(program, vs); gl.attachShader(program, fs); gl.linkProgram(program); if (!gl.getProgramParameter(program, gl.LINK_STATUS)) throw Error(gl.getProgramInfoLog(program)); gl.deleteShader(vs); gl.deleteShader(fs); gl.useProgram(program);
  const uniforms = Object.fromEntries(['voiceCrop', 'size', 'amount', 'mouthWidth', 'mood', 'headMotion', 'bodyMotion', 'gaze', 'browSide', 'kind', 'region', 'extent', 'tex0', 'tex1', 'tex2', 'crop', 'layer', 'teethHide', 'fullArt', 'eyeA', 'eyeB', 'fade', 'blend', 'outFlip', 'faceBlink', 'eyeExt', 'eyeHide', 'lipHide', 'detailBand', 'flatAmount', 'motion0', 'motion1', 'motion2', 'motion3', 'poseW', 'gazeW', 'tex3', 'exprMix', 'shapeMode', 'useMultiDonor', 'cavityScale'].map(k => [k, gl.getUniformLocation(program, k)]));
  const topAttribute = gl.getAttribLocation(program, 'cavityTop');
  const mouthUVAttributes = ['mouthCoords0', 'mouthCoords1', 'mouthCoords2'].map(name => gl.getAttribLocation(program, name));
  const mouthPaintUniform = gl.getUniformLocation(program, 'mouthPaint');
  const mouthNeutralUniform = gl.getUniformLocation(program, 'mouthNeutral'), mouthFaceUniform = gl.getUniformLocation(program, 'mouthFace');
  const mouthWeightsUniform = gl.getUniformLocation(program, 'mouthWeights[0]');
  const textures = {};
  gl.pixelStorei(gl.UNPACK_PREMULTIPLY_ALPHA_WEBGL, true);
  gl.pixelStorei(gl.UNPACK_COLORSPACE_CONVERSION_WEBGL, gl.NONE);
  // Hair in front of the eyes must not travel with the eyelid. Lift those strands into a fixed
  // layer and give the eyelid mesh a copy of the base image with the strands filled with skin.
  const eyeRigs = (asset.rig?.features || []).filter(f => f.kind === 'eye');
  const hairSplit = separateHair(base, eyeRigs, 7);
  const sources = { ...images, clean: hairSplit.clean, hair: hairSplit.hair };
  // Optional per-vowel mouth artwork, cropped to the mouth box (offset on the idle canvas).
  const vowelArt = Object.fromEntries(Object.entries(asset.vowels || {}).filter(([v]) => images['vowel_' + v]).map(([v, item]) => ['vowel_' + v, item]));
  // Opening expression mouths (teeth, mouth interior): cropped like the vowels, drawn on a measured mouth mesh.
  for (const [n, item] of Object.entries(asset.expressions || {})) if (item.mouth && images['exprmouth_' + n]) vowelArt['exprmouth_' + n] = item.mouth;
  // Per-expression vowel mouths: a second target and texture for each neutral vowel mesh.
  const exprVowelArt = {}; for (const [n, items] of Object.entries(asset.expressionVowels || {})) for (const [v, item] of Object.entries(items)) if (images[`exprvowel_${n}_${v}`]) { exprVowelArt[`exprvowel_${n}_${v}`] = item; sources[`exprvowel_${n}_${v}`] = images[`exprvowel_${n}_${v}`]; }
  for (const key of Object.keys(vowelArt)) sources[key] = images[key];
  // Conversational upper-lip layers per expression: one RGBA crop per speech opening, registered to
  // the mouth mesh by its upper border and inner rim (31 refined columns, corner to corner).
  const upperLips = {};
  for (const [n, keys] of Object.entries(asset.upperLipLayers || {})) {
    const loaded = keys.map((k, i) => ({ ...k, key: `upperlip_${n}_${i}` })).filter(k => images[k.key]);
    if (loaded.length === keys.length) { upperLips[n] = loaded; for (const k of loaded) sources[k.key] = images[k.key]; }
  }
  // Optional drawn expression face patches (scripts/build_expressions.py), same placement scheme.
  const expressionArt = Object.fromEntries(Object.entries(asset.expressions || {}).filter(([n]) => images['expr_' + n]).map(([n, item]) => ['expr_' + n, item]));
  for (const key of Object.keys(expressionArt)) sources[key] = images[key];
  // Morph fields: an expression without one cannot be drawn seamlessly, so it is not offered.
  for (const key of Object.keys(expressionArt)) { if (!images['exprflow_' + key.slice(5)]) { delete expressionArt[key]; delete sources[key]; continue; } sources['exprflow_' + key.slice(5)] = images['exprflow_' + key.slice(5)]; }
  const flowKeys = Object.keys(expressionArt).map(k => 'exprflow_' + k.slice(5));
  const motionKeys = [0, 1, 2, 3].filter(i => images['motion' + i]).length === 4 ? ['motion0', 'motion1', 'motion2', 'motion3'] : [];
  for (const key of motionKeys) sources[key] = images[key];
  for (const key of ['idle', 'talking', 'blink', 'clean', 'hair', ...Object.keys(vowelArt), ...Object.keys(expressionArt), ...flowKeys, ...motionKeys, ...Object.keys(exprVowelArt), ...Object.values(upperLips).flat().map(k => k.key)]) {
    const tex = gl.createTexture(); textures[key] = tex; gl.bindTexture(gl.TEXTURE_2D, tex);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_S, gl.CLAMP_TO_EDGE); gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_T, gl.CLAMP_TO_EDGE);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MIN_FILTER, gl.LINEAR_MIPMAP_LINEAR); gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MAG_FILTER, gl.LINEAR);
    gl.pixelStorei(gl.UNPACK_PREMULTIPLY_ALPHA_WEBGL, !key.startsWith('exprflow_') && !key.startsWith('motion'));
    gl.texImage2D(gl.TEXTURE_2D, 0, gl.RGBA, gl.RGBA, gl.UNSIGNED_BYTE, sources[key] || base); gl.generateMipmap(gl.TEXTURE_2D);
    gl.pixelStorei(gl.UNPACK_PREMULTIPLY_ALPHA_WEBGL, true);
  }
  const mouthUnits = Int32Array.from(VOWELS, (_, i) => 8 + i);
  gl.uniform1iv(gl.getUniformLocation(program, 'mouthVowels[0]'), mouthUnits);
  VOWELS.forEach((v, i) => {
    gl.activeTexture(gl.TEXTURE0 + mouthUnits[i]);
    gl.bindTexture(gl.TEXTURE_2D, textures['exprvowel_thinking_' + v] || textures.idle);
  });
  gl.activeTexture(gl.TEXTURE0);
  function mesh(source, target, columns, rows, inner = [], cavity = null, target2 = null, lip = null) {
    const N = 11;
    const t2 = target2 || target;
    const data = []; for (let i = 0; i < source.length / 2; i++)data.push(source[i * 2], source[i * 2 + 1], target[i * 2], target[i * 2 + 1], cavity ? cavity[i] : inner.includes(Math.floor(i / columns)) ? 1 : 0, i % columns > 0 && i % columns < columns - 1 && i >= columns && i < columns * (rows - 1) ? 1 : 0, t2[i * 2], t2[i * 2 + 1], lip ? lip[i] : 0, target[i * 2], target[i * 2 + 1]);
    const indices = []; for (let r = 0; r < rows - 1; r++)for (let c = 0; c < columns - 1; c++) { const a = r * columns + c, b = a + 1, d = a + columns, e = d + 1; indices.push(a, b, d, b, e, d); }
    const vao = gl.createVertexArray(); gl.bindVertexArray(vao);
    const vertices = gl.createBuffer(), array = new Float32Array(data); gl.bindBuffer(gl.ARRAY_BUFFER, vertices); gl.bufferData(gl.ARRAY_BUFFER, array, gl.DYNAMIC_DRAW);
    for (const [name, n, offset] of [['source', 2, 0], ['target', 2, 8], ['cavity', 1, 16], ['flex', 1, 20], ['target2', 2, 24], ['lipw', 1, 32], ['shape', 2, 36]]) { const location = gl.getAttribLocation(program, name); if (location < 0) continue; gl.enableVertexAttribArray(location); gl.vertexAttribPointer(location, n, gl.FLOAT, false, N * 4, offset); }
    const elements = gl.createBuffer(); gl.bindBuffer(gl.ELEMENT_ARRAY_BUFFER, elements); gl.bufferData(gl.ELEMENT_ARRAY_BUFFER, new Uint16Array(indices), gl.STATIC_DRAW);
    return { vao, vertices, elements, count: indices.length, array, N };
  }
  const grid = []; for (let r = 0; r < 43; r++)for (let c = 0; c < 35; c++)grid.push(c / 34 * base.width, r / 42 * base.height);
  const background = mesh(grid, grid, 35, 43);
  // Registered feature grids are coarse (9x6) and the mouth corners sit one column from the
  // fixed border, so narrowing the mouth stretched a thin strip of cheek and hair. Add a fixed
  // margin around the mouth and subdivide bilinearly so width/smile deformation can fade out
  // smoothly before the patch edge. Landmark vertices keep their original positions.
  // The expression part of an expression-vowel mouth with its opening removed: every column keeps
  // its horizontal move and follows the upper lip's seam lift (corners lift into a smile while the
  // center never drops down with the lower jaw), fading to the fixed top and bottom rows.
  function closedSmile(f){
    const a=f.mesh.array,N=f.mesh.N,C=f.columns,R=f.rows,factor=3,out=new Float32Array(C*R*2);
    const kLo=6,kHi=C-1-6,mid=(kLo+kHi)/2,half=Math.max(1,(kHi-kLo)/2);
    for(let k=0;k<C;k++){
      const o2=(2*factor*C+k)*N,dyTop=a[o2+7]-a[o2+1];
      const corner=Math.min(1,Math.abs(k-mid)/half);
      const dy=dyTop<0?dyTop*(.32+.68*corner*corner):dyTop*.15*corner*corner;
      for(let r=0;r<R;r++){
        const rr=r/factor,o=(r*C+k)*N,w=rr<1?rr:rr<=4?1:Math.max(0,5-rr);
        out[(r*C+k)*2]=(a[o+6]-a[o])*w;out[(r*C+k)*2+1]=dy*w;
      }
    }
    return out;
  }
  function refinePoints(src, coords, C, R, margin = 45, factor = 3) {
    let s = src.slice(), d = coords.slice();
    if (margin > 0) {
      const s2 = [], d2 = [];
      for (let r = 0; r < R; r++) {
        const [lx, ly] = [s[(r * C) * 2], s[(r * C) * 2 + 1]], [rx, ry] = [s[(r * C + C - 1) * 2], s[(r * C + C - 1) * 2 + 1]];
        s2.push(lx - margin, ly); d2.push(lx - margin, ly);
        for (let k = 0; k < C; k++) { s2.push(s[(r * C + k) * 2], s[(r * C + k) * 2 + 1]); d2.push(d[(r * C + k) * 2], d[(r * C + k) * 2 + 1]); }
        s2.push(rx + margin, ry); d2.push(rx + margin, ry);
      }
      s = s2; d = d2; C += 2;
    }
    const C2 = (C - 1) * factor + 1, R2 = (R - 1) * factor + 1, out = new Float32Array(C2 * R2 * 2);
    for (let r = 0; r < R2; r++)for (let k = 0; k < C2; k++) {
      const r0 = Math.min(R - 2, Math.floor(r / factor)), k0 = Math.min(C - 2, Math.floor(k / factor)), v = r / factor - r0, u = k / factor - k0;
      const mix = (o) => { const a = d[((r0 * C + k0) * 2) + o], b = d[((r0 * C + k0 + 1) * 2) + o], e = d[(((r0 + 1) * C + k0) * 2) + o], g = d[(((r0 + 1) * C + k0 + 1) * 2) + o]; return (a * (1 - u) + b * u) * (1 - v) + (e * (1 - u) + g * u) * v; };
      out[(r * C2 + k) * 2] = mix(0); out[(r * C2 + k) * 2 + 1] = mix(1);
    }
    return out;
  }
  function refine(f, margin, factor, lidSkin = 0, lipFullness = null) {
    let C = f.columns, R = f.rows, src = f.source.slice(), dst = f.target.slice(), dst2 = (f.target2 || f.target).slice();
    if (lipFullness && R === 6) {
      // The rig moves both edges of each lip by the same amount, so opening only bends two
      // constant-thickness bands. Move the outer edges (row 1: upper vermilion border,
      // row 4: lower lip's lower edge) further than the inner edges. The rig displacement is
      // largest at the centre and ~0 at the corners, so lips get fuller in the middle and
      // stay thin at the corners, as in the open-mouth artwork.
      const [up, down] = lipFullness;
      for (let k = 0; k < C; k++) {
        const i1 = C + k, i2 = 2 * C + k, i3 = 3 * C + k, i4 = 4 * C + k;
        dst[i1 * 2 + 1] = src[i1 * 2 + 1] + (dst[i2 * 2 + 1] - src[i2 * 2 + 1]) * up;
        dst[i4 * 2 + 1] = src[i4 * 2 + 1] + (dst[i3 * 2 + 1] - src[i3 * 2 + 1]) * down;
      }
    }
    let cav = Array.from({ length: C * R }, (_, i) => (f.interior_rows || []).includes(Math.floor(i / C)) ? 1 : 0);
    if (lidSkin > 0 && R > 2) {
      // Eyes: the fixed top row sits only ~16 px above the upper lash line, so closing the lid
      // stretched the crease and any hair crossing it 3x. Pin a row just above the lash line:
      // only the featureless lid skin below it stretches; crease and hair stay put.
      const s2 = [], d2 = [], c2 = [];
      for (let k = 0; k < C; k++) { s2.push(src[k * 2], src[k * 2 + 1]); d2.push(dst[k * 2], dst[k * 2 + 1]); c2.push(cav[k]); }
      for (let k = 0; k < C; k++) { const x = src[(C + k) * 2], y = Math.max(src[k * 2 + 1] + 2, src[(C + k) * 2 + 1] - lidSkin); s2.push(x, y); d2.push(x, y); c2.push(-1); }
      for (let i = C; i < C * R; i++) { s2.push(src[i * 2], src[i * 2 + 1]); d2.push(dst[i * 2], dst[i * 2 + 1]); c2.push(i < 2 * C ? -1 : cav[i]); }
      src = s2; dst = d2; cav = c2; R += 1;
    }
    if (margin > 0) {
      const s2 = [], d2 = [], c2 = [], e2 = [];
      for (let r = 0; r < R; r++) {
        const row = k => [src[(r * C + k) * 2], src[(r * C + k) * 2 + 1]];
        const [lx, ly] = row(0), [rx, ry] = row(C - 1);
        s2.push(lx - margin, ly); d2.push(lx - margin, ly); e2.push(lx - margin, ly); c2.push(0);
        for (let k = 0; k < C; k++) { s2.push(src[(r * C + k) * 2], src[(r * C + k) * 2 + 1]); d2.push(dst[(r * C + k) * 2], dst[(r * C + k) * 2 + 1]); e2.push(dst2[(r * C + k) * 2], dst2[(r * C + k) * 2 + 1]); c2.push(cav[r * C + k]); }
        s2.push(rx + margin, ry); d2.push(rx + margin, ry); e2.push(rx + margin, ry); c2.push(0);
      }
      src = s2; dst = d2; dst2 = e2; cav = c2; C += 2;
    }
    const C2 = (C - 1) * factor + 1, R2 = (R - 1) * factor + 1, s = [], d = [], c = [], d2 = [], lw = [];
    const inner = f.interior_rows || [], rLo = Math.min(...inner) - 1, rHi = Math.max(...inner) + 1, kLo = margin > 0 ? 2 : 1, kHi = C - 1 - kLo;
    const at = (arr, r, k, n) => arr[(r * C + k) * n];
    for (let r = 0; r < R2; r++)for (let k = 0; k < C2; k++) {
      const r0 = Math.min(R - 2, Math.floor(r / factor)), k0 = Math.min(C - 2, Math.floor(k / factor)), v = r / factor - r0, u = k / factor - k0;
      const mix = (arr, n, o) => { const a = arr[((r0 * C + k0) * n) + o], b = arr[((r0 * C + k0 + 1) * n) + o], e = arr[(((r0 + 1) * C + k0) * n) + o], g = arr[(((r0 + 1) * C + k0 + 1) * n) + o]; return (a * (1 - u) + b * u) * (1 - v) + (e * (1 - u) + g * u) * v; };
      s.push(mix(src, 2, 0), mix(src, 2, 1)); d.push(mix(dst, 2, 0), mix(dst, 2, 1)); d2.push(mix(dst2, 2, 0), mix(dst2, 2, 1)); c.push(mix(cav, 1, 0)); lw.push(r >= rLo * factor && r <= rHi * factor && k >= kLo * factor && k <= kHi * factor ? 1 : 0);
    }
    let ex = 0, ey = 0; for (let i = 0; i < s.length; i += 2) { ex = Math.max(ex, Math.abs(s[i] - f.region[0])); ey = Math.max(ey, Math.abs(s[i + 1] - f.region[1])); }
    return { ...f, columns: C2, rows: R2, extent: [ex, ey], mesh: mesh(s, d, C2, R2, [], c, f.target2 ? d2 : null, f.kind === 'mouth' ? lw : null) };
  }
  // Measured mouth rigs (scripts/build_mouth_rig.py) already follow the open lips.
  const LIP_FULLNESS = asset.lipFullness || null;
  const features = (asset.rig?.features || []).filter(f => f.kind !== 'mouth' || f.image === 'talking' || vowelArt[f.image]).map(f => f.kind === 'mouth' ? refine(f, 45, 3, 0, LIP_FULLNESS) : refine(f, 0, 2, 7));
  const vowelFeatures = Object.fromEntries(features.filter(f => f.kind === 'mouth' && f.image.startsWith('vowel_')).map(f => [f.image.slice(6), f]));
  const rawVowel = Object.fromEntries((asset.rig?.features || []).filter(f => f.kind === 'mouth' && f.image.startsWith('vowel_') && vowelArt[f.image]).map(f => [f.image.slice(6), f]));
  const exprVowelFeatures = {};
  for (const [key, item] of Object.entries(exprVowelArt)) { const [, name, v] = key.split('_'); if (!rawVowel[v]) continue; (exprVowelFeatures[name] ||= {})[v] = refine({ ...rawVowel[v], target2: item.target2 }, 45, 3, 0, LIP_FULLNESS); }
  // One closed mouth per expression (the mean of its vowels' closed shapes): a smiling speaker keeps
  // one smile between syllables; how a vowel narrows or widens comes only with its opening.
  for (const group of Object.values(exprVowelFeatures)) {
    const shapes = Object.values(group).map(closedSmile);
    if (!shapes.length || shapes.some(s => s.length !== shapes[0].length)) continue;
    const mean = new Float32Array(shapes[0].length); for (const s of shapes) for (let i = 0; i < s.length; i++)mean[i] += s[i] / shapes.length;
    for (const f of Object.values(group)) f.closedSmile = mean;
  }
  const exprMouths = Object.fromEntries(features.filter(f => f.kind === 'mouth' && f.image.startsWith('exprmouth_')).map(f => [f.image.slice(10), f]));
  const hasVowels = Object.keys(vowelFeatures).length > 0;
  const donor = vowelFeatures.A || Object.values(vowelFeatures)[0];
  const compatible = f => f && f.rows === donor.rows && f.columns === donor.columns;
  const registered = donor && donor.interior_rows?.join() === '2,3' && Object.values(vowelFeatures).every(compatible);
  // The authored thinking set has a measured closed mouth and matching crops for all vowels.
  // Models with only the older face patch keep their existing mouth path.
  const thinkingRest = registered && compatible(exprMouths.thinking) &&
    VOWELS.every(v => compatible(vowelFeatures[v]) && compatible(exprVowelFeatures.thinking?.[v])) ? exprMouths.thinking : null;
  const lipCenter = donor ? (3 * 2 * donor.columns + Math.floor(donor.columns / 2)) * 11 : 0;
  const mouthUVStride = 2 * (1 + VOWELS.length);
  const rawMouthUVs = asset.mouthTextureCoords || null;
  const allNeutralVowels = registered && VOWELS.every(v => compatible(vowelFeatures[v]) && vowelArt['vowel_' + v]);
  const thinkingUV = thinkingRest ? buildMouthUVBuffer([
    [thinkingRest, vowelArt[thinkingRest.image], 2, rawMouthUVs?.exprMouths?.thinking],
    ...VOWELS.map(v => [exprVowelFeatures.thinking[v], exprVowelArt['exprvowel_thinking_' + v], 6, rawMouthUVs?.expressionVowels?.thinking?.[v]])
  ]) : null;
  const neutralVowelUV = allNeutralVowels ? buildMouthUVBuffer([
    [vowelFeatures.A, vowelArt.vowel_A, 2, rawMouthUVs?.vowels?.A],
    ...VOWELS.map(v => [vowelFeatures[v], vowelArt['vowel_' + v], 2, rawMouthUVs?.vowels?.[v]])
  ]) : null;
  const thinkingWeights = new Float32Array(VOWELS.length);
  const neutralWeights = new Float32Array(VOWELS.length);
  let boundVowelSet = 'thinking';
  function buildMouthUVBuffer(samples) {
    const count = donor.mesh.array.length / donor.mesh.N, uv = new Float32Array(count * mouthUVStride);
    for (const [index, [feature, crop, start, rawCoords]] of samples.entries()) {
      const { array, N } = feature.mesh, [x, y] = crop.offset;
      const rawC = (feature.columns - 1) / 3 - 1, rawR = (feature.rows - 1) / 3 + 1;
      const raw = rawCoords && rawCoords.length === rawC * rawR * 2 ? refinePoints(feature.source, rawCoords, rawC, rawR, 45, 3) : null;
      for (let i = 0; i < count; i++) {
        const at = i * mouthUVStride + index * 2;
        const px = raw ? raw[i * 2] : array[i * N + start], py = raw ? raw[i * 2 + 1] : array[i * N + start + 1];
        uv[at] = (px - x) / crop.width;
        uv[at + 1] = (py - y) / crop.height;
      }
    }
    const buffer = gl.createBuffer();
    gl.bindBuffer(gl.ARRAY_BUFFER, buffer); gl.bufferData(gl.ARRAY_BUFFER, uv, gl.STATIC_DRAW);
    return buffer;
  }
  function bindMouthUV(mesh, buffer, vowelSet) {
    if (mesh.boundMouthUV !== buffer) {
      gl.bindBuffer(gl.ARRAY_BUFFER, buffer);
      for (const [i, attribute] of mouthUVAttributes.entries()) {
        gl.enableVertexAttribArray(attribute);
        gl.vertexAttribPointer(attribute, 4, gl.FLOAT, false, mouthUVStride * 4, i * 16);
      }
      mesh.boundMouthUV = buffer;
    }
    if (boundVowelSet !== vowelSet) {
      VOWELS.forEach((v, i) => {
        gl.activeTexture(gl.TEXTURE0 + mouthUnits[i]);
        gl.bindTexture(gl.TEXTURE_2D, textures[(vowelSet === 'thinking' ? 'exprvowel_thinking_' : 'vowel_') + v] || textures.idle);
      });
      boundVowelSet = vowelSet;
    }
  }
  function bindThinkingMouth(mesh, { weight, amount }) {
    bindMouthUV(mesh, thinkingUV, 'thinking');
    gl.activeTexture(gl.TEXTURE2); gl.bindTexture(gl.TEXTURE_2D, textures[thinkingRest.image]);
    gl.activeTexture(gl.TEXTURE1);
    gl.uniform2f(mouthPaintUniform, weight, amount);
    gl.uniform1fv(mouthWeightsUniform, thinkingWeights);
  }
  // The native grid is subdivided three times. Rows 2 and 3 bound the cavity.
  function drawRegisteredMouth(poses, art, paint, vowelShares) {
    const expression = art.find(([name]) => compatible(exprVowelFeatures[name]?.[donor.image.slice(6)]));
    const name = expression?.[0], voice = name && exprVowelFeatures[name][donor.image.slice(6)];
    const { mesh } = voice || donor, data = mesh.array;
    writeMouthLandmarks(mesh, poses);
    const bottom = lipCenter + 3 * donor.columns * mesh.N;
    const gap = (a, i) => a[bottom + i] - a[lipCenter + i];
    const closed = paint && thinkingRest.mesh.array;
    // The measured closed seam can be a pixel wider than neutral. Only actual opening reveals teeth.
    const seam = closed ? (gap(closed, 3) - gap(closed, 1)) * paint.weight * (1 - paint.amount) : 0;
    const opening = Math.max(0, gap(data, 10) - gap(data, 1) - seam);
    gl.bindBuffer(gl.ARRAY_BUFFER, mesh.vertices); gl.bufferSubData(gl.ARRAY_BUFFER, 0, data);
    gl.activeTexture(gl.TEXTURE0); gl.bindTexture(gl.TEXTURE_2D, art.length ? faceTexture : textures.idle);
    gl.activeTexture(gl.TEXTURE13); gl.bindTexture(gl.TEXTURE_2D, textures.idle);
    gl.uniform1i(mouthNeutralUniform, 13); gl.uniform1f(mouthFaceUniform, art.length ? 1 : 0);
    gl.activeTexture(gl.TEXTURE1); gl.bindTexture(gl.TEXTURE_2D, textures[donor.image]);
    gl.bindVertexArray(mesh.vao);
    const useMulti = !paint && Boolean(neutralVowelUV);
    if (useMulti) {
      bindMouthUV(mesh, neutralVowelUV, 'neutral');
      gl.uniform1fv(mouthWeightsUniform, vowelShares);
    }
    gl.uniform1f(uniforms.useMultiDonor, useMulti ? 1 : 0);
    // Attenuate wide-smile teeth plate on rounded vowels (U, O) so pursed lips cover the grin teeth.
    const roundedCavity = Math.max(0, 1 - (.85 * (vowelShares[2] || 0) + .50 * (vowelShares[4] || 0)));
    gl.uniform1f(uniforms.cavityScale, roundedCavity);
    if (voice) {
      const key = 'exprvowel_' + name + '_' + donor.image.slice(6), texture = exprVowelArt[key];
      if (!mesh.cavityAnchors) {
        // Per vertex: raw PNG upper-teeth top in this column (raw row 2 + 1.5 px into teeth, never the
        // pink/brown upper-lip rim), corner taper across, and resting upper-lip Y.
        const count = data.length / mesh.N, C2 = voice.columns, kL = 6, kR = ((C2 - 1) / 3 - 2) * 3, anchors = new Float32Array(3 * count);
        const rawC = (C2 - 1) / 3 - 1, rawR = (voice.rows - 1) / 3 + 1;
        const rawEV = rawMouthUVs?.expressionVowels?.[name]?.[donor.image.slice(6)];
        const rawRef = rawEV && rawEV.length === rawC * rawR * 2 ? refinePoints(voice.source, rawEV, rawC, rawR, 45, 3) : null;
        for (let i = 0; i < count; i++) {
          const k = i % C2, topY = rawRef ? rawRef[(6 * C2 + k) * 2 + 1] + 1.5 : data[(6 * C2 + k) * mesh.N + 7] + 1.5;
          anchors[3 * i] = topY;
          anchors[3 * i + 1] = Math.max(0, Math.min(1, Math.min(k - kL, kR - k) / 6));
          anchors[3 * i + 2] = data[(6 * C2 + k) * mesh.N + 1];
        }
        mesh.cavityAnchors = gl.createBuffer(); gl.bindBuffer(gl.ARRAY_BUFFER, mesh.cavityAnchors);
        gl.bufferData(gl.ARRAY_BUFFER, anchors, gl.STATIC_DRAW);
        gl.enableVertexAttribArray(topAttribute); gl.vertexAttribPointer(topAttribute, 3, gl.FLOAT, false, 0, 0);
      }
      gl.activeTexture(gl.TEXTURE3); gl.bindTexture(gl.TEXTURE_2D, textures[key]);
      gl.uniform4fv(uniforms.voiceCrop, [...texture.offset, texture.width, texture.height]);
      gl.uniform1f(uniforms.exprMix, expression[1]);
      const donorGap = Math.max(1, gap(data, 7));
      gl.uniform1f(uniforms.flatAmount, Math.min(1, Math.max(.12, opening / donorGap)));
    }
    if (paint) bindThinkingMouth(mesh, paint);
    gl.uniform1i(uniforms.kind, 7); gl.uniform1f(uniforms.amount, opening);
    gl.uniform4fv(uniforms.crop, cropOf(donor)); gl.uniform4fv(uniforms.region, donor.region);
    gl.drawElements(gl.TRIANGLES, mesh.count, gl.UNSIGNED_SHORT, 0);
    gl.uniform1f(uniforms.exprMix, 0); gl.uniform1f(uniforms.flatAmount, 0); gl.uniform1f(uniforms.useMultiDonor, 0); gl.uniform1f(uniforms.cavityScale, 1);
    if (paint) gl.uniform2f(mouthPaintUniform, 0, 0);
    if (upperLipState && upperLips[upperLipState.name]) drawUpperLip((voice || donor).columns, mesh, data, upperLips[upperLipState.name], upperLipState);
  }
  // The neutral vowel's upper lip, stretched into a smile, read as a uniform rubber band. While an
  // expression with upper-lip layers shows, its own generated upper lip is laid over the mouth: the
  // two keys around the speech opening are each put on the current inner rim (their own thickness
  // above it, plus the band joining the lip to the skin) and mixed premultiplied, then composited
  // once. The inner rim clips it, so teeth and the lower lip stay the mesh's.
  let upperLipState = null, lipProgram = null, lipVao = null, lipBuffer = null, lipIndex = null, lipUniforms = null, lipCount = 0;
  const LIP_COLUMNS = 31, LIP_BAND = 8;
  // Compiled when the surface is made, not on the first smiling word (a stall at reply onset).
  function drawUpperLipSetup() {
    if (lipProgram) return;
    const vs = '#version 300 es\nin vec2 pos; in vec2 uvA; in vec2 uvB; uniform vec2 size; uniform float outFlip; out vec2 vA; out vec2 vB; void main(){vA=uvA;vB=uvB;gl_Position=vec4(pos.x/size.x*2.-1.,(1.-pos.y/size.y*2.)*outFlip,0.,1.);}';
    const fs = '#version 300 es\nprecision highp float; in vec2 vA; in vec2 vB; uniform sampler2D texA; uniform sampler2D texB; uniform float mixF; uniform float weight; out vec4 color; void main(){color=mix(texture(texA,vA),texture(texB,vB),mixF)*weight;}';
    lipProgram = gl.createProgram();
    for (const [type, src] of [[gl.VERTEX_SHADER, vs], [gl.FRAGMENT_SHADER, fs]]) { const sh = gl.createShader(type); gl.shaderSource(sh, src); gl.compileShader(sh); if (!gl.getShaderParameter(sh, gl.COMPILE_STATUS)) throw Error('PNGTUBER_UPPER_LIP_SHADER ' + gl.getShaderInfoLog(sh)); gl.attachShader(lipProgram, sh); }
    gl.linkProgram(lipProgram); if (!gl.getProgramParameter(lipProgram, gl.LINK_STATUS)) throw Error('PNGTUBER_UPPER_LIP_PROGRAM');
    lipUniforms = Object.fromEntries(['size', 'outFlip', 'texA', 'texB', 'mixF', 'weight'].map(k => [k, gl.getUniformLocation(lipProgram, k)]));
    lipVao = gl.createVertexArray(); gl.bindVertexArray(lipVao); lipBuffer = gl.createBuffer(); gl.bindBuffer(gl.ARRAY_BUFFER, lipBuffer);
    gl.bufferData(gl.ARRAY_BUFFER, LIP_COLUMNS * 3 * 6 * 4, gl.DYNAMIC_DRAW);
    [['pos', 0], ['uvA', 8], ['uvB', 16]].forEach(([name, off]) => { const loc = gl.getAttribLocation(lipProgram, name); gl.enableVertexAttribArray(loc); gl.vertexAttribPointer(loc, 2, gl.FLOAT, false, 24, off); });
    const idx = []; for (let j = 0; j < LIP_COLUMNS - 1; j++)for (let r = 0; r < 2; r++) { const a = r * LIP_COLUMNS + j, b = a + 1, c = a + LIP_COLUMNS, d = c + 1; idx.push(a, b, c, b, d, c); }
    lipIndex = gl.createBuffer(); gl.bindBuffer(gl.ELEMENT_ARRAY_BUFFER, lipIndex); gl.bufferData(gl.ELEMENT_ARRAY_BUFFER, new Uint16Array(idx), gl.STATIC_DRAW); lipCount = idx.length;
  }
  function drawUpperLip(cols, mesh, data, keys, state) {
    const w = state.weight; if (!(w > .001)) return;
    drawUpperLipSetup();
    const a = Math.max(0, Math.min(1, state.amount)); let i = 0; while (i < keys.length - 2 && a > keys[i + 1].opening) i++;
    const A = keys[i], B = keys[i + 1], f = Math.max(0, Math.min(1, (a - A.opening) / Math.max(1e-6, B.opening - A.opening)));
    const N = mesh.N;
    const verts = new Float32Array(LIP_COLUMNS * 3 * 6);
    for (let j = 0; j < LIP_COLUMNS; j++) {
      const v = (6 * cols + 6 + j) * N, ix = data[v + 9], iy = data[v + 10];
      // this key's lip, and its outer border, relative to its own inner rim
      const tx = (1 - f) * (A.outer[j][0] - A.inner[j][0]) + f * (B.outer[j][0] - B.inner[j][0]), ty = (1 - f) * (A.outer[j][1] - A.inner[j][1]) + f * (B.outer[j][1] - B.inner[j][1]);
      const rows = [[ix + tx, iy + ty - LIP_BAND, -LIP_BAND], [ix + tx, iy + ty, 0], [ix, iy, null]];
      rows.forEach(([x, y, band], r) => {
        const o = (r * LIP_COLUMNS + j) * 6; verts[o] = x; verts[o + 1] = y;
        for (const [K, off] of [[A, 2], [B, 4]]) {
          const px = band === null ? K.inner[j][0] : K.outer[j][0], py = band === null ? K.inner[j][1] : K.outer[j][1] + (band || 0);
          verts[o + off] = (px - K.offset[0]) / K.width; verts[o + off + 1] = (py - K.offset[1]) / K.height;
        }
      });
    }
    gl.useProgram(lipProgram); gl.bindVertexArray(lipVao); gl.bindBuffer(gl.ARRAY_BUFFER, lipBuffer); gl.bufferSubData(gl.ARRAY_BUFFER, 0, verts);
    gl.uniform2f(lipUniforms.size, base.width, base.height); gl.uniform1f(lipUniforms.outFlip, -1); gl.uniform1f(lipUniforms.mixF, f); gl.uniform1f(lipUniforms.weight, w);
    gl.activeTexture(gl.TEXTURE14); gl.bindTexture(gl.TEXTURE_2D, textures[A.key]); gl.uniform1i(lipUniforms.texA, 14);
    gl.activeTexture(gl.TEXTURE15); gl.bindTexture(gl.TEXTURE_2D, textures[B.key]); gl.uniform1i(lipUniforms.texB, 15);
    gl.drawElements(gl.TRIANGLES, lipCount, gl.UNSIGNED_SHORT, 0);
    gl.useProgram(program); gl.activeTexture(gl.TEXTURE0);
  }
  // The drawn vowels are fully articulated; conversational speech rarely opens that far.
  const MOUTH_REACH = Number.isFinite(asset.mouthReach) ? Math.max(.3, Math.min(1, asset.mouthReach)) : .7;
  const cropOf = f => { const v = vowelArt[f.image]; return v ? [v.offset[0], v.offset[1], v.width, v.height] : [0, 0, base.width, base.height]; };
  // Which artwork to show when the caller gives no vowel weights (rest mouths, stills).
  function vowelsFor(width) { if (vowelFeatures.U && width < .72) return { U: 1 }; if (vowelFeatures.O && width < .92) return { O: 1 }; if (vowelFeatures.I && width > 1.09) return { I: 1 }; if (vowelFeatures.E && width > 1.02) return { E: 1 }; return { A: 1 }; }
  const hairBoxes = hairSplit.boxes.map(([x, y, w, h], i) => { const p = [x, y, x + w, y, x, y + h, x + w, y + h]; return { ...mesh(p, p, 2, 2), region: hairSplit.regions[i] }; });
  // Brows reuse the same idle texture. All patch borders remain fixed.
  const eyes = features.filter(f => f.kind === 'eye'), faceX = eyes.reduce((sum, f) => sum + f.region[0], 0) / Math.max(1, eyes.length);
  const brows = eyes.map(f => {
    const [cx, eyeY, rx, ry] = f.region, region = [cx, eyeY - ry * 1.7, rx * 1.08, ry * .88], points = [];
    for (let r = 0; r < 5; r++)for (let c = 0; c < 9; c++)points.push(region[0] + region[2] * (c / 4 - 1), region[1] + region[3] * (r / 2 - 1));
    return { region, side: cx < faceX ? 1 : -1, mesh: mesh(points, points, 9, 5) };
  });
  // Expression patches: a fixed grid over each crop so the head/body transform bends them with the face.
  const expressionPatches = Object.fromEntries(Object.entries(expressionArt).map(([key, item]) => {
    const [ox, oy] = item.offset, w = item.width, h = item.height, points = [];
    for (let r = 0; r < 9; r++)for (let c = 0; c < 9; c++)points.push(ox + w * c / 8, oy + h * r / 8);
    // detailBand: above it the expression only moves the sharp neutral texture (see pngtuber.py).
    return [key.slice(5), { ...mesh(points, points, 9, 9), crop: [ox, oy, w, h], detail: Array.isArray(item.detailBand) ? item.detailBand : [0, 0] }];
  }));
  // How far the idle glance turns the eyes, as a fraction of the gaze keyframe.
  const IDLE_GLANCE_REACH = .8;
  const mouthZone = features.find(f => f.kind === 'mouth' && f.image.startsWith('vowel_')) || features.find(f => f.kind === 'mouth');
  const eyeZones = eyes.length === 2 ? eyes.map(f => f.region) : [[0, 0, 1, 1], [0, 0, 1, 1]];
  // While blinking the patch steps aside only inside the lid rig's own mesh, which shows the same face.
  const eyeExtents = eyes.length === 2 ? [...eyes[0].extent, ...eyes[1].extent] : [1e-3, 1e-3, 1e-3, 1e-3];
  // Offscreen face with the expression applied (no head motion, hair-free lids): while blinking, the
  // eyelid rig closes this face instead of switching to the neutral eye (no double lid lines).
  const faceTexture = gl.createTexture(); gl.bindTexture(gl.TEXTURE_2D, faceTexture);
  gl.texImage2D(gl.TEXTURE_2D, 0, gl.RGBA8, base.width, base.height, 0, gl.RGBA, gl.UNSIGNED_BYTE, null);
  for (const [k, v] of [[gl.TEXTURE_MIN_FILTER, gl.LINEAR], [gl.TEXTURE_MAG_FILTER, gl.LINEAR], [gl.TEXTURE_WRAP_S, gl.CLAMP_TO_EDGE], [gl.TEXTURE_WRAP_T, gl.CLAMP_TO_EDGE]]) gl.texParameteri(gl.TEXTURE_2D, k, v);
  const faceBuffer = gl.createFramebuffer(); gl.bindFramebuffer(gl.FRAMEBUFFER, faceBuffer); gl.framebufferTexture2D(gl.FRAMEBUFFER, gl.COLOR_ATTACHMENT0, gl.TEXTURE_2D, faceTexture, 0); gl.bindFramebuffer(gl.FRAMEBUFFER, null);
  // The face is composed in neutral layout (no head/body motion) into composeTexture; the final pass
  // warps it by the head-tilt / head-turn / gaze fields (scripts/build_motion.py) and then applies
  // the acting head/body motion, so expression, vowels and blinks all move with the pose.
  let needsMipmaps = false;
  const composeTexture = gl.createTexture(); gl.bindTexture(gl.TEXTURE_2D, composeTexture);
  gl.texImage2D(gl.TEXTURE_2D, 0, gl.RGBA8, base.width, base.height, 0, gl.RGBA, gl.UNSIGNED_BYTE, null);
  for (const [k, v] of [[gl.TEXTURE_MIN_FILTER, gl.LINEAR], [gl.TEXTURE_MAG_FILTER, gl.LINEAR], [gl.TEXTURE_WRAP_S, gl.CLAMP_TO_EDGE], [gl.TEXTURE_WRAP_T, gl.CLAMP_TO_EDGE]]) gl.texParameteri(gl.TEXTURE_2D, k, v);
  gl.generateMipmap(gl.TEXTURE_2D);
  const composeBuffer = gl.createFramebuffer(); gl.bindFramebuffer(gl.FRAMEBUFFER, composeBuffer); gl.framebufferTexture2D(gl.FRAMEBUFFER, gl.COLOR_ATTACHMENT0, gl.TEXTURE_2D, composeTexture, 0); gl.bindFramebuffer(gl.FRAMEBUFFER, null);
  const finalPoints = []; for (let r = 0; r < 129; r++)for (let c = 0; c < 129; c++)finalPoints.push(c / 128 * base.width, r / 128 * base.height);
  const finalMesh = mesh(finalPoints, finalPoints, 129, 129);
  const identityMotion = gl.createTexture(); gl.bindTexture(gl.TEXTURE_2D, identityMotion); gl.texImage2D(gl.TEXTURE_2D, 0, gl.RGBA, 1, 1, 0, gl.RGBA, gl.UNSIGNED_BYTE, new Uint8Array([128, 128, 128, 128]));
  [0, 1, 2, 3].forEach(i => gl.uniform1i(uniforms['motion' + i], 4 + i)); gl.uniform4f(uniforms.poseW, 0, 0, 0, 0); gl.uniform4f(uniforms.gazeW, 0, 0, 0, 0);
  gl.uniform1f(uniforms.outFlip, 1); gl.uniform1f(uniforms.blend, 1); gl.uniform1f(uniforms.faceBlink, 0); gl.uniform1f(uniforms.flatAmount, 0);
  gl.uniform2f(uniforms.size, base.width, base.height); gl.uniform1f(uniforms.layer, 1); gl.uniform4f(uniforms.crop, 0, 0, base.width, base.height); gl.uniform1i(uniforms.tex0, 0); gl.uniform1i(uniforms.tex1, 1); gl.uniform1i(uniforms.tex2, 2); gl.uniform1i(uniforms.tex3, 3); gl.uniform1f(uniforms.exprMix, 0);
  gl.enable(gl.BLEND); gl.blendFunc(gl.ONE, gl.ONE_MINUS_SRC_ALPHA);
  const ext = gl.getExtension('WEBGL_debug_renderer_info');
  const gpu = ext ? gl.getParameter(ext.UNMASKED_RENDERER_WEBGL) : gl.getParameter(gl.RENDERER);
  let draws = 0;
  // Drawn expression faces, morphed along their flow field. Geometry follows the weight t (neutral
  // pushed t of the way, the expression pulled back 1-t); the colour switches to the expression
  // early (t 0.02..0.12, where the warped expression is already almost on the neutral geometry), so a held, milder expression is the drawn face relaxed towards neutral, never a
  // half-transparent blend of two faces. The mouth zone morphs back and yields to the articulated
  // mouth while talking (mouthArt=0). In a change both are near neutral; the stronger is drawn
  // first and the weaker blended over it by its share.
  // offscreen: the drawn wink eye is left out there, so the lid rig closes an open eye.
  function drawArt(art, neutralTexture, mouthArt, hideA, hideB, offscreen = false) {
    gl.uniform1i(uniforms.kind, 5); gl.uniform2f(uniforms.fade, mouthArt, 1);
    gl.uniform4fv(uniforms.eyeA, eyeZones[0]); gl.uniform4fv(uniforms.eyeB, eyeZones[1]); gl.uniform4fv(uniforms.eyeExt, eyeExtents);
    if (mouthZone) { gl.uniform4fv(uniforms.region, mouthZone.region); gl.uniform2fv(uniforms.extent, mouthZone.extent); } else { gl.uniform4f(uniforms.region, -1e4, -1e4, 1, 1); gl.uniform2f(uniforms.extent, 1, 1); }
    gl.activeTexture(gl.TEXTURE0); gl.bindTexture(gl.TEXTURE_2D, neutralTexture);
    const total = art.reduce((sum, [, w]) => sum + w, 0);
    for (const [i, [name, w]] of art.entries()) {
      const m = expressionPatches[name]; gl.uniform1f(uniforms.blend, i === 0 ? 1 : w / total);
      gl.uniform2f(uniforms.eyeHide, offscreen && name === 'wink' ? 1 : hideA, hideB); gl.uniform1f(uniforms.lipHide, registered || exprMouths[name] ? 1 : 0);
      gl.activeTexture(gl.TEXTURE2); gl.bindTexture(gl.TEXTURE_2D, textures['exprflow_' + name]);
      gl.activeTexture(gl.TEXTURE1); gl.bindTexture(gl.TEXTURE_2D, textures['expr_' + name]);
      gl.uniform1f(uniforms.layer, w); gl.uniform4fv(uniforms.crop, m.crop); gl.uniform2fv(uniforms.detailBand, m.detail);
      gl.bindVertexArray(m.vao); gl.drawElements(gl.TRIANGLES, m.count, gl.UNSIGNED_SHORT, 0);
    }
    gl.uniform1f(uniforms.layer, 1); gl.uniform1f(uniforms.blend, 1); gl.uniform1f(uniforms.lipHide, 0);
  }
  function render(open = 0, blink = 0, mouthWidth = 1, expression = {}, movement = {}, vowels = null) {
    open = Math.max(0, Math.min(1, open)); blink = Math.max(0, Math.min(1, blink));
    const { smile = 0, browRaise = 0, browTilt = 0, wide = 0 } = expression;
    const safe = (value, limit) => Number.isFinite(value) ? Math.max(-limit, Math.min(limit, value)) : 0;
    // An expression may carry its own glance (thinking), added to the acting gaze.
    // The acting gaze (e.g. reading a comment) and an expression's own glance do not add up past the
    // larger of the two, or the iris ends up pressed into the eye corner.
    const combine = (a, b) => { a = Number.isFinite(a) ? a : 0; b = Number.isFinite(b) ? b : 0; const s = a + b, m = Math.max(Math.abs(a), Math.abs(b)); return Math.max(-m, Math.min(m, s)); };
    const gx = safe(combine(movement.gazeX, expression.gazeX), 6), gy = safe(combine(movement.gazeY, expression.gazeY), 3);
    const art = Object.entries(expression).filter(([k, w]) => k.startsWith('art_') && w > .003 && expressionPatches[k.slice(4)]).map(([k, w]) => [k.slice(4), Math.min(1, w)]).sort((a, b) => b[1] - a[1]).slice(0, 2);
    const mouthArt = Number.isFinite(expression.mouthArt) ? Math.max(0, Math.min(1, expression.mouthArt)) : 1;
    const thinkingWeight = thinkingRest ? (art.find(([name]) => name === 'thinking')?.[1] || 0) : 0;
    const legacyArt = thinkingWeight > 0 ? art.filter(([name]) => name !== 'thinking') : art;
    // Wink: the first eye closes on the lid rig (see expressions.js); the drawn eye steps aside.
    const wink = Math.max(0, Math.min(1, (Number.isFinite(expression.winkA) ? expression.winkA : 0) * 1.6));
    const eyeBlink = [Math.max(blink, wink), blink];
    const blinkFace = art.length > 0 && (blink > 0 || wink > 0) && features.some(f => f.kind === 'eye');
    // Opening mouths: the mouth mesh opens the expression face (cheeks included) from the closed neutral lips.
    const openMouths = registered ? [] : art.filter(([n, w]) => exprMouths[n] && w * mouthArt > .003);
    // The registered mouth grid warps the face under it, so with an expression drawn it must warp the
    // expression face (smile cheeks, blush), not the neutral portrait: the offscreen face is built.
    const mouthOnArt = registered && art.length > 0;
    if (blinkFace || openMouths.length || mouthOnArt) {
      gl.bindFramebuffer(gl.FRAMEBUFFER, faceBuffer); gl.viewport(0, 0, base.width, base.height); gl.clearColor(0, 0, 0, 0); gl.clear(gl.COLOR_BUFFER_BIT);
      gl.uniform1f(uniforms.outFlip, -1); gl.uniform2f(uniforms.gaze, 0, 0); gl.uniform3f(uniforms.headMotion, 0, 0, 0); gl.uniform3f(uniforms.bodyMotion, 0, 0, 0);
      gl.activeTexture(gl.TEXTURE0); gl.bindTexture(gl.TEXTURE_2D, textures.clean); gl.uniform1i(uniforms.kind, 0); gl.uniform1f(uniforms.amount, 0); gl.bindVertexArray(background.vao); gl.drawElements(gl.TRIANGLES, background.count, gl.UNSIGNED_SHORT, 0);
      drawArt(art, textures.clean, registered ? 1 : mouthArt, 0, 0, true);
      gl.bindFramebuffer(gl.FRAMEBUFFER, null); gl.uniform1f(uniforms.outFlip, 1);
    }
    gl.uniform2f(uniforms.gaze, 0, 0);
    const headMotion = [safe(movement.headTilt, .06), safe(movement.headX, 8), safe(movement.headY, 10)], bodyMotion = [safe(movement.bodyTilt, .012), safe(movement.breath, 3), safe(movement.bodyX, 4)];
    gl.uniform3f(uniforms.headMotion, 0, 0, 0); gl.uniform3f(uniforms.bodyMotion, 0, 0, 0);
    gl.uniform4f(uniforms.mood, smile, browRaise, browTilt, wide);
    gl.uniform1f(uniforms.mouthWidth, Math.max(.55, Math.min(1.15, mouthWidth)));
    gl.bindFramebuffer(gl.FRAMEBUFFER, composeBuffer); gl.viewport(0, 0, base.width, base.height); gl.uniform1f(uniforms.outFlip, -1); gl.clearColor(0, 0, 0, 0); gl.clear(gl.COLOR_BUFFER_BIT);
    gl.activeTexture(gl.TEXTURE2); gl.bindTexture(gl.TEXTURE_2D, textures.idle);
    gl.activeTexture(gl.TEXTURE1); gl.bindTexture(gl.TEXTURE_2D, textures.idle);
    gl.activeTexture(gl.TEXTURE0); gl.bindTexture(gl.TEXTURE_2D, textures.idle); gl.uniform1i(uniforms.kind, 0); gl.uniform1f(uniforms.amount, 0); gl.bindVertexArray(background.vao); gl.drawElements(gl.TRIANGLES, background.count, gl.UNSIGNED_SHORT, 0);
    if (browRaise !== 0 || browTilt !== 0) for (const b of brows) {
      gl.uniform1i(uniforms.kind, 3); gl.uniform1f(uniforms.amount, 0); gl.uniform1f(uniforms.browSide, b.side); gl.uniform4fv(uniforms.region, b.region); gl.bindVertexArray(b.mesh.vao); gl.drawElements(gl.TRIANGLES, b.mesh.count, gl.UNSIGNED_SHORT, 0);
    }
    // The registered mesh follows every active vowel. The older layered draw uses its two strongest
    // crops. With no vowel artwork, the single talking image supplies the mouth.
    let mouthLayers = [], registeredPoses = [], mouthPaint = null; upperLipState = null;

    if (hasVowels) {
      const input = vowels && typeof vowels === 'object' && Object.keys(vowels).some(k => vowelFeatures[k]) ? vowels : vowelsFor(mouthWidth);
      const active = Object.entries(input)
        .filter(([v, w]) => VOWELS.includes(v) && vowelFeatures[v] && Number.isFinite(w) && w > (registered ? 0 : .03))
        .sort((a, b) => b[1] - a[1]);
      const weights = registered ? active : active.slice(0, 2);
      if (!weights.length) weights.push([Object.keys(vowelFeatures)[0], 1]);
      const total = weights.reduce((sum, [, w]) => sum + w, 0), aperture = weights.reduce((sum, [v, w]) => sum + w * (MOUTH_SHAPES[v]?.aperture || 1), 0) / total;
      // A planned mouth (known reply text) gives the opening directly as a fraction of the drawn vowel.
      const planned = vowels && Number.isFinite(vowels.amount) ? Math.max(0, Math.min(1, vowels.amount)) : null;
      const amount = planned !== null ? planned : Math.min(1, open / Math.max(.25, aperture)) * MOUTH_REACH;
      // While an expression with its own vowel mouths is showing, each vowel mesh morphs towards that
      // expression's vowel (shape and colour) by the expression weight.
      const lead = legacyArt.find(([n]) => exprVowelFeatures[n]), mixTo = lead ? Math.min(1, lead[1]) : 0;
      mouthLayers = weights.map(([v, w], i) => { const ev = lead && exprVowelFeatures[lead[0]][v]; return { vowel: v, f: ev || vowelFeatures[v], amount, layer: i === 0 ? 1 : w / total, share: w / total, width: 1, teeth: 0, exprMix: ev ? mixTo : 0, exprTex: ev ? textures[`exprvowel_${lead[0]}_${v}`] : null }; });
      neutralWeights.fill(0);
      for (const m of mouthLayers) { const idx = VOWELS.indexOf(m.vowel); if (idx >= 0) neutralWeights[idx] = m.share; }
      // Attenuate wide-smile upper-lip overlay on rounded vowels (U, O) so puckered lips keep their shape.
      const roundedLip = Math.max(0, 1 - (.85 * (neutralWeights[2] || 0) + .45 * (neutralWeights[4] || 0)));
      upperLipState = lead && upperLips[lead[0]] ? { name: lead[0], weight: smoothstep01((mixTo - .15) / .35) * (1 - Math.max(0, Math.min(1, expression.restPose ?? mouthArt))) * roundedLip, amount } : null;
    } else { neutralWeights.fill(0); neutralWeights[0] = 1; const f = features.find(x => x.kind === 'mouth'); if (f) mouthLayers = [{ f, amount: open, layer: 1, width: mouthWidth, teeth: 1 }]; }
    if (registered) {
      const rest = Math.max(0, Math.min(1, expression.restPose ?? mouthArt));
      const legacyWeight = 1 - thinkingWeight;
      for (const [name, weight] of legacyArt) {
        const f = exprMouths[name];
        if (compatible(f) && rest > 0) registeredPoses.push({ mesh: f.mesh, weight: rest * weight * legacyWeight, express: 0 });
      }
      // A missing resting mouth can still use the procedural resting aperture.
      const hasRestRig = legacyArt.some(([name]) => compatible(exprMouths[name]));
      // A smiling speaker keeps the smile when the lips meet: the expression's corner position, width
      // and lift (its vowel shape with the opening closed) follow the expression weight, and only the
      // opening follows the voice. Before, the corners jumped between the neutral closed mouth and the
      // wide expression mouth at every syllable.
      for (const m of mouthLayers) {
        const speech = m.share * (hasRestRig ? 1 - rest : 1), closed = m.exprMix > 0 && m.f.closedSmile ? m.share * m.exprMix * (1 - m.amount) * (1 - mouthArt) : 0;
        if (closed > 0) registeredPoses.push({ disp: m.f.closedSmile, weight: closed * legacyWeight });
        if (m.amount * speech > 0) registeredPoses.push({ mesh: m.f.mesh, weight: m.amount * speech * legacyWeight, express: m.exprMix });
      }
      if (thinkingWeight > 0) {
        const amount = mouthLayers[0].amount * (1 - rest);
        // Hold the actual closed contour across consonants and pauses. Interpolate between that
        // mesh and the current vowels. Their textures follow the same projected landmarks.
        registeredPoses.push({ mesh: thinkingRest.mesh, weight: thinkingWeight * (1 - amount), express: 0 });
        thinkingWeights.fill(0);
        for (const m of mouthLayers) {
          thinkingWeights[VOWELS.indexOf(m.vowel)] = m.share;
          registeredPoses.push({
            mesh: exprVowelFeatures.thinking[m.vowel].mesh,
            weight: thinkingWeight * amount * m.share, express: 1
          });
        }
        mouthPaint = { weight: thinkingWeight, amount };
      }
    }
    if (!registered && (open > 0 || smile !== 0)) for (const m of mouthLayers) {
      const f = m.f;
      gl.uniform1i(uniforms.shapeMode, 0);
      gl.uniform1f(uniforms.exprMix, m.exprMix || 0); gl.activeTexture(gl.TEXTURE3); gl.bindTexture(gl.TEXTURE_2D, m.exprTex || textures.idle);
      gl.uniform1f(uniforms.mouthWidth, Math.max(.55, Math.min(1.15, m.width)));
      gl.activeTexture(gl.TEXTURE0); gl.bindTexture(gl.TEXTURE_2D, textures.idle);
      gl.activeTexture(gl.TEXTURE1); gl.bindTexture(gl.TEXTURE_2D, textures[f.image]);
      gl.uniform1i(uniforms.kind, 1); gl.uniform1f(uniforms.amount, m.amount); gl.uniform1f(uniforms.layer, m.layer); gl.uniform1f(uniforms.teethHide, m.teeth); gl.uniform1f(uniforms.fullArt, m.teeth ? 0 : 1); gl.uniform4fv(uniforms.crop, cropOf(f)); gl.uniform4fv(uniforms.region, f.region); gl.uniform2fv(uniforms.extent, f.extent); gl.bindVertexArray(f.mesh.vao); gl.drawElements(gl.TRIANGLES, f.mesh.count, gl.UNSIGNED_SHORT, 0);
    }
    gl.uniform1f(uniforms.exprMix, 0); gl.uniform1i(uniforms.shapeMode, 0);
    let eyeIndex = 0;
    for (const f of features) {
      if (f.kind !== 'eye') continue; const value = eyeBlink[Math.min(1, eyeIndex++)]; if (value === 0 && wide === 0 && smile <= 0 && gx === 0 && gy === 0) continue;
      // With an expression the stretched lid band comes from the hair-free neutral lid skin.
      gl.uniform1f(uniforms.faceBlink, blinkFace ? 1 : 0); if (blinkFace) gl.uniform2f(uniforms.gaze, 0, 0); gl.activeTexture(gl.TEXTURE2); gl.bindTexture(gl.TEXTURE_2D, blinkFace ? textures.clean : textures.idle);
      gl.activeTexture(gl.TEXTURE0); gl.bindTexture(gl.TEXTURE_2D, blinkFace ? faceTexture : textures.clean);
      gl.activeTexture(gl.TEXTURE1); gl.bindTexture(gl.TEXTURE_2D, textures[f.image]);
      gl.uniform1i(uniforms.kind, 2); gl.uniform1f(uniforms.amount, value); gl.uniform1f(uniforms.layer, 1); gl.uniform4fv(uniforms.region, f.region); gl.uniform2fv(uniforms.extent, f.extent); gl.bindVertexArray(f.mesh.vao); gl.drawElements(gl.TRIANGLES, f.mesh.count, gl.UNSIGNED_SHORT, 0);
    }
    if (features.some(f => f.kind === 'eye') && (blink > 0 || wink > 0 || wide !== 0 || smile > 0 || gx !== 0 || gy !== 0)) {
      gl.activeTexture(gl.TEXTURE0); gl.bindTexture(gl.TEXTURE_2D, textures.hair); gl.uniform1i(uniforms.kind, 4); gl.uniform1f(uniforms.amount, Math.max(blink, wink));
      for (const m of hairBoxes) { gl.uniform4fv(uniforms.region, m.region); gl.bindVertexArray(m.vao); gl.drawElements(gl.TRIANGLES, m.count, gl.UNSIGNED_SHORT, 0); }
    }
    // Drawn expression faces go on top, morphed along their flow field: at weight t the neutral
    // face is pushed t of the way and the expression pulled back (1-t), then blended, so features
    // move into place. The mouth zone morphs back to neutral and yields to the articulated mouth
    // while talking (mouthArt=0); the eyes morph back and yield to the blink rig while blinking.
    // During a change both expressions are close to neutral (expressions.js); the stronger one is
    // drawn first and the weaker one blended over it by its share, so neither pops.
    // While blinking the eye zone is left to the lid rig, which already closes the expression face.
    gl.uniform2f(uniforms.gaze, 0, 0); gl.uniform1f(uniforms.faceBlink, 0);
    if (art.length) drawArt(art, textures.idle, registered ? 1 : mouthArt, blinkFace ? 1 : 0, blinkFace && blink > 0 ? 1 : 0);
    if (registered && (registeredPoses.length || art.length)) drawRegisteredMouth(registeredPoses, legacyArt, mouthPaint, neutralWeights);
    if (openMouths.length) {
      const total = art.reduce((sum, [, w]) => sum + w, 0);
      // The crop is colour-matched cheeks included, so the whole mesh moves to it at one rate (corner shadows
      // reaching past the measured corner would otherwise be mixed with skin into a grey smudge).
      gl.uniform1f(uniforms.mouthWidth, 1); gl.uniform1f(uniforms.teethHide, 0); gl.uniform1f(uniforms.fullArt, 1); gl.uniform1i(uniforms.kind, 1); gl.uniform1f(uniforms.flatAmount, 1);
      for (const [i, [name, w]] of openMouths.entries()) {
        const f = exprMouths[name];
        gl.activeTexture(gl.TEXTURE0); gl.bindTexture(gl.TEXTURE_2D, faceTexture);
        gl.activeTexture(gl.TEXTURE1); gl.bindTexture(gl.TEXTURE_2D, textures[f.image]);
        gl.uniform1f(uniforms.amount, Math.min(1, w * mouthArt)); gl.uniform1f(uniforms.layer, i === 0 ? 1 : w / total); gl.uniform4fv(uniforms.crop, cropOf(f)); gl.uniform4fv(uniforms.region, f.region); gl.uniform2fv(uniforms.extent, f.extent);
        gl.bindVertexArray(f.mesh.vao); gl.drawElements(gl.TRIANGLES, f.mesh.count, gl.UNSIGNED_SHORT, 0);
      }
      gl.uniform1f(uniforms.layer, 1); gl.uniform1f(uniforms.flatAmount, 0);
    }
    const idleWeight = safe(movement.idleGlance, 1);
    // Idle glance: drawn with the gaze field (the measured gaze keyframe moves only the iris and lids
    // together). The 9 generated eye frames are no longer drawn: their late frames press the iris
    // into the eye corner and cross-fading two frames showed two irises.
    // Final pass: pose and gaze fields, then the acting head/body motion, onto the canvas.
    gl.bindFramebuffer(gl.FRAMEBUFFER, null); gl.bindTexture(gl.TEXTURE_2D, null);
    gl.activeTexture(gl.TEXTURE0); gl.bindTexture(gl.TEXTURE_2D, composeTexture); if (needsMipmaps) gl.generateMipmap(gl.TEXTURE_2D);
    [0, 1, 2, 3].forEach(i => { gl.activeTexture(gl.TEXTURE4 + i); gl.bindTexture(gl.TEXTURE_2D, motionKeys.length ? textures['motion' + i] : identityMotion); });
    const clamp1 = x => Number.isFinite(x) ? Math.max(-1.2, Math.min(1.2, x)) : 0, tilt = clamp1(movement.poseTilt), turn = clamp1(movement.poseTurn);
    // Gaze fields come from open neutral eyes: they fade out as the lids close.
    const eyeOpen = 1 - Math.max(blink, wink * .6), gxw = clamp1(gx / 7 - idleWeight * IDLE_GLANCE_REACH) * eyeOpen, gyw = clamp1(gy / 2.6) * eyeOpen;
    gl.uniform4f(uniforms.poseW, Math.max(0, -tilt), Math.max(0, tilt), Math.max(0, -turn), Math.max(0, turn));
    gl.uniform4f(uniforms.gazeW, Math.max(0, -gxw), Math.max(0, gxw), Math.max(0, -gyw), Math.max(0, gyw));
    gl.uniform3f(uniforms.headMotion, ...headMotion); gl.uniform3f(uniforms.bodyMotion, ...bodyMotion); gl.uniform1f(uniforms.outFlip, 1);
    gl.viewport(0, 0, canvas.width, canvas.height); gl.clearColor(0, 0, 0, 0); gl.clear(gl.COLOR_BUFFER_BIT);
    gl.uniform1i(uniforms.kind, 6); gl.bindVertexArray(finalMesh.vao); gl.drawElements(gl.TRIANGLES, finalMesh.count, gl.UNSIGNED_SHORT, 0);
    gl.activeTexture(gl.TEXTURE0);
    draws++; return canvas;
  }
  if (Object.keys(upperLips).length) { drawUpperLipSetup(); gl.bindVertexArray(null); gl.useProgram(program); }
  return {
    canvas, gpu, render, features, brows, hairSplit, expressionNames: new Set(Object.keys(expressionPatches)), resize(edge) {
      canvas.width = Math.min(base.width, edge); canvas.height = Math.round(canvas.width * base.height / base.width);
      needsMipmaps = canvas.width < base.width * .7;
      gl.activeTexture(gl.TEXTURE0); gl.bindTexture(gl.TEXTURE_2D, composeTexture);
      gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MIN_FILTER, needsMipmaps ? gl.LINEAR_MIPMAP_LINEAR : gl.LINEAR);
    }, readPixels() { const pixels = new Uint8Array(canvas.width * canvas.height * 4); gl.readPixels(0, 0, canvas.width, canvas.height, gl.RGBA, gl.UNSIGNED_BYTE, pixels); return pixels; }, snapshot: () => ({ mode: 'registered-linear-mesh', source_size: [base.width, base.height], surface_size: [canvas.width, canvas.height], gpu, draws, features: features.length, texture_filter: 'linear-mipmap-linear', color_blending: 'linear-light-premultiplied', mouth_width_range: [.55, 1.15], mouth_mesh: 'margin45-subdivided3-radial-falloff', mouth_material: thinkingRest ? 'registered-thinking-vowels' : registered ? 'single-registered-donor' : 'legacy', idle_glance: 'gaze-field', mouth_artwork: hasVowels ? 'per-vowel:' + Object.keys(vowelFeatures).join('') : 'single-talking', mouth_reach: MOUTH_REACH, expression_layer: 'mouth-corners-eyelids-brows', eyebrow_patches: brows.length, hair_layer_pixels: hairSplit.pixels, body_mesh: [35, 43], gaze_limit_pixels: [6, 3], head_motion: 'neck-weighted-2d', torso_motion: 'root-weighted-2d' }), dispose() { if (thinkingUV) gl.deleteBuffer(thinkingUV); if (neutralVowelUV) gl.deleteBuffer(neutralVowelUV); for (const group of Object.values(exprVowelFeatures)) for (const f of Object.values(group)) if (f.mesh.cavityAnchors) gl.deleteBuffer(f.mesh.cavityAnchors); for (const m of [background, ...features.map(f => f.mesh), ...brows.map(b => b.mesh), ...hairBoxes]) { gl.deleteBuffer(m.vertices); gl.deleteBuffer(m.elements); gl.deleteVertexArray(m.vao); } for (const t of Object.values(textures)) gl.deleteTexture(t); gl.deleteProgram(program); gl.getExtension('WEBGL_lose_context')?.loseContext(); }
  };
}

// Accumulate displacements from each pose's own rest grid. Crops from the
// same face may have different fixed outer borders (especially under the chin).
export function writeMouthLandmarks(mesh, poses) {
  const vertices = mesh.array, stride = mesh.N;
  for (let i = 0; i < vertices.length; i += stride) {
    vertices[i + 9] = vertices[i];
    vertices[i + 10] = vertices[i + 1];
  }
  for (const { mesh: target, weight, express, disp } of poses) {
    if (disp) {
      for (let i = 0, k = 0; i < vertices.length; i += stride, k += 2) {
        vertices[i + 9] += weight * disp[k];
        vertices[i + 10] += weight * disp[k + 1];
      }
      continue;
    }
    const pose = target.array;
    for (let i = 0; i < vertices.length; i += stride) {
      vertices[i + 9] += weight * (pose[i + 2] - pose[i] + express * (pose[i + 6] - pose[i + 2]));
      vertices[i + 10] += weight * (pose[i + 3] - pose[i + 1] + express * (pose[i + 7] - pose[i + 3]));
    }
  }
}

function smoothstep01(x) { const t = Math.max(0, Math.min(1, x)); return t * t * (3 - 2 * t); }

// Split thin hair strands crossing the upper-eyelid band (between the pinned crease row and the
// upper lash line, taken from the eye rig rows) from the skin under them. Strands are darker than
// the local average; lashes are excluded by position, not colour, because both are equally dark.
function separateHair(base, eyes, lidSkin = 7) {
  const W = base.width, H = base.height;
  const clean = document.createElement('canvas'); clean.width = W; clean.height = H;
  const cg = clean.getContext('2d', { willReadFrequently: true }); cg.drawImage(base, 0, 0);
  const hair = document.createElement('canvas'); hair.width = W; hair.height = H; const hg = hair.getContext('2d');
  const boxes = [], regions = []; let pixels = 0;
  for (const f of eyes) {
    const C = f.columns, src = f.source, colX = k => src[k * 2], rowY = (r, k) => src[(r * C + k) * 2 + 1];
    if (f.rows < 3) continue;
    const along = (r, x) => { for (let k = 0; k < C - 1; k++) { const a = colX(k), b = colX(k + 1); if (x >= a && x <= b) return rowY(r, k) + (rowY(r, k + 1) - rowY(r, k)) * (x - a) / Math.max(1e-6, b - a); } return rowY(r, x < colX(0) ? 0 : C - 1); };
    const xa = Math.ceil(colX(1)), xb = Math.floor(colX(C - 2)), top = [], bottom = []; let ya = Infinity, yb = -Infinity;
    for (let x = xa; x <= xb; x++) { const t = Math.max(rowY(0, 0) + 2, along(1, x) - 13), b = along(2, x) - 2; top.push(t); bottom.push(b); ya = Math.min(ya, t); yb = Math.max(yb, b); }
    const pad = 8, X0 = Math.max(0, xa - pad), Y0 = Math.max(0, Math.floor(ya) - pad), w = Math.min(W, xb + pad + 1) - X0, h = Math.min(H, Math.ceil(yb) + pad + 1) - Y0;
    if (w <= 0 || h <= 0 || !(yb > ya)) continue;
    const data = cg.getImageData(X0, Y0, w, h), px = data.data, L = new Float32Array(w * h);
    for (let i = 0; i < w * h; i++)L[i] = (px[i * 4] * .299 + px[i * 4 + 1] * .587 + px[i * 4 + 2] * .114) / 255;
    const I = new Float64Array((w + 1) * (h + 1));
    for (let y = 0; y < h; y++) { let row = 0; for (let x = 0; x < w; x++) { row += L[y * w + x]; I[(y + 1) * (w + 1) + x + 1] = I[y * (w + 1) + x + 1] + row; } }
    const r = 6, mean = (x, y) => { const x0 = Math.max(0, x - r), y0 = Math.max(0, y - r), x1 = Math.min(w, x + r + 1), y1 = Math.min(h, y + r + 1); return (I[y1 * (w + 1) + x1] - I[y0 * (w + 1) + x1] - I[y1 * (w + 1) + x0] + I[y0 * (w + 1) + x0]) / ((x1 - x0) * (y1 - y0)); };
    const mask = new Float32Array(w * h);
    for (let y = 0; y < h; y++)for (let x = 0; x < w; x++) {
      const gx = X0 + x, gy = Y0 + y; if (gx < xa || gx > xb) continue; const t = top[gx - xa], b = bottom[gx - xa]; if (gy < t || gy > b) continue;
      const edge = Math.min(1, (gy - t) / 3, (b - gy) / 3, (gx - xa) / 4, (xb - gx) / 4);
      mask[y * w + x] = Math.min(1, Math.max(0, (mean(x, y) - L[y * w + x] - .03) / .08)) * edge;
    }
    // Grow by one pixel so anti-aliased strand edges go with the strand.
    const grown = mask.slice();
    for (let y = 1; y < h - 1; y++)for (let x = 1; x < w - 1; x++) { const i = y * w + x; grown[i] = Math.max(mask[i], .6 * Math.max(mask[i - 1], mask[i + 1], mask[i - w], mask[i + w])); }
    const layer = hg.createImageData(w, h), out = layer.data;
    for (let i = 0; i < w * h; i++) { out[i * 4] = px[i * 4]; out[i * 4 + 1] = px[i * 4 + 1]; out[i * 4 + 2] = px[i * 4 + 2]; out[i * 4 + 3] = Math.round(grown[i] * 255); if (grown[i] > .15) pixels++; }
    hg.putImageData(layer, X0, Y0);
    // Fill strand pixels with the surrounding skin (iterative neighbour averaging).
    const rgb = new Float32Array(w * h * 3); for (let i = 0; i < w * h; i++)for (let c = 0; c < 3; c++)rgb[i * 3 + c] = px[i * 4 + c];
    for (let n = 0; n < 60; n++)for (let y = 1; y < h - 1; y++)for (let x = 1; x < w - 1; x++) {
      const i = y * w + x; if (grown[i] <= .15) continue;
      for (let c = 0; c < 3; c++)rgb[i * 3 + c] = (rgb[(i - 1) * 3 + c] + rgb[(i + 1) * 3 + c] + rgb[(i - w) * 3 + c] + rgb[(i + w) * 3 + c]) / 4;
    }
    for (let i = 0; i < w * h; i++)if (grown[i] > .15) { const k = Math.min(1, grown[i] * 1.5); for (let c = 0; c < 3; c++)px[i * 4 + c] = Math.round(px[i * 4 + c] * (1 - k) + rgb[i * 3 + c] * k); }
    // Lid skin that gets stretched while closing: plain skin taken just above the lashes, so the
    // stretch shows a smooth eyelid instead of a smeared brow edge or crease.
    const skin = (x) => { let r = 0, g = 0, b = 0, n = 0; for (let dx = -2; dx <= 2; dx++)for (let dy = 2; dy <= 4; dy++) { const xx = x + dx - X0, yy = Math.round(along(1, x + dx) + dy) - Y0; if (xx < 0 || yy < 0 || xx >= w || yy >= h) continue; const i = (yy * w + xx) * 4; r += px[i]; g += px[i + 1]; b += px[i + 2]; n++; } return n ? [r / n, g / n, b / n] : null; };
    const raw = []; for (let x = xa; x <= xb; x++)raw.push(skin(x));
    const fill = raw.map((_, i) => { let r = 0, g = 0, b = 0, n = 0; for (let j = Math.max(0, i - 8); j <= Math.min(raw.length - 1, i + 8); j++) { const c = raw[j]; if (!c) continue; r += c[0]; g += c[1]; b += c[2]; n++; } return n ? [r / n, g / n, b / n] : null; });
    for (let x = xa; x <= xb; x++) {
      const c = fill[x - xa]; if (!c) continue; const e = Math.min(1, (x - xa) / 10, (xb - x) / 10); const y0 = Math.round(along(1, x) - lidSkin) - 1, y1 = Math.round(along(1, x)) + 1;
      for (let y = y0; y <= y1; y++) { const xx = x - X0, yy = y - Y0; if (yy < 0 || yy >= h) continue; const i = (yy * w + xx) * 4; for (let k = 0; k < 3; k++)px[i + k] = px[i + k] * (1 - e) + c[k] * e; }
    }
    cg.putImageData(data, X0, Y0);
    boxes.push([X0, Y0, w, h]); regions.push(f.region);
  }
  return { clean, hair, boxes, regions, pixels };
}

// Mouth plan (built before playout from the known reply text, see ravora/speech.py plan_mouth):
// each key holds a shape at an opening from start to end. Hold the middle of each key and ease
// (smoothstep) between neighbours, so the mouth reaches each key without snapping.
export function sampleMouthPlan(plan, time) {
  const none = { amount: 0, vowels: null, shape: 'closed' };
  if (!Array.isArray(plan) || !plan.length || !Number.isFinite(time)) return none;
  const hold = k => { const d = k.end - k.start, m = k.shape === 'closed' ? .2 : .3; return [k.start + d * m, k.end - d * m]; };
  let low = 0, high = plan.length;
  while (low < high) { const mid = (low + high) >> 1; if (hold(plan[mid])[1] < time) low = mid + 1; else high = mid; }
  const next = plan[low], prev = plan[low - 1];
  const pose = k => ({ amount: k.shape === 'closed' ? 0 : k.amount, shape: k.shape });
  if (!next) { if (!prev) return none; const p = pose(prev), fade = Math.min(1, Math.max(0, (time - hold(prev)[1]) / .12)); return { amount: p.amount * (1 - fade), vowels: p.shape === 'closed' ? null : { [p.shape]: 1 }, shape: p.shape }; }
  const [a] = hold(next);
  if (time >= a || !prev) { const p = pose(next); if (!prev && time < a) { const k = Math.min(1, Math.max(0, 1 - (a - time) / .12)); return { amount: p.amount * k, vowels: p.shape === 'closed' ? null : { [p.shape]: 1 }, shape: p.shape }; } return { amount: p.amount, vowels: p.shape === 'closed' ? null : { [p.shape]: 1 }, shape: p.shape }; }
  const b = hold(prev)[1], u0 = Math.min(1, Math.max(0, (time - b) / Math.max(1e-3, a - b))), u = u0 * u0 * (3 - 2 * u0);
  const p = pose(prev), n = pose(next); const amount = p.amount + (n.amount - p.amount) * u;
  let vowels;
  if (p.shape === 'closed' && n.shape === 'closed') vowels = null;
  else if (p.shape === 'closed') vowels = { [n.shape]: 1 };
  else if (n.shape === 'closed') vowels = { [p.shape]: 1 };
  else if (p.shape === n.shape) vowels = { [p.shape]: 1 };
  else vowels = { [p.shape]: 1 - u, [n.shape]: u };
  return { amount, vowels, shape: u < .5 ? p.shape : n.shape };
}

// Visual categories approximate English speech; they are not Japanese phoneme recognition.
const MOUTH_SHAPES = { A: { aperture: 1, width: 1 }, I: { aperture: .30, width: 1.13 }, U: { aperture: .44, width: .66 }, E: { aperture: .56, width: 1.05 }, O: { aperture: .82, width: .76 }, N: { aperture: .28, width: .97 }, closed: { aperture: 0, width: 1 } };
export function mouthShape(name) { return { ...(MOUTH_SHAPES[name] || MOUTH_SHAPES.N), shape: name }; }
export function sampleViseme(track, time) {
  let low = 0, high = track.length;
  while (low < high) { const mid = (low + high) >> 1; if (track[mid].end <= time) low = mid + 1; else high = mid; }
  const current = track[low];
  if (!current || time < current.start) return mouthShape('closed');
  let result = mouthShape(current.shape);
  const blend = (a, b, t) => ({ aperture: a.aperture + (b.aperture - a.aperture) * t, width: a.width + (b.width - a.width) * t, shape: current.shape });
  const before = track[low - 1], after = track[low + 1];
  // Symmetric short coarticulation at actual phoneme boundaries; gaps remain closed.
  if (before && Math.abs(before.end - current.start) < .00001) {
    const radius = Math.min(.022, (current.end - current.start) * .3, (before.end - before.start) * .3);
    if (radius > 0 && time - current.start < radius) result = blend(mouthShape(before.shape), result, .5 + .5 * (time - current.start) / radius);
  }
  if (after && Math.abs(current.end - after.start) < .00001) {
    const radius = Math.min(.022, (current.end - current.start) * .3, (after.end - after.start) * .3);
    if (radius > 0 && current.end - time < radius) result = blend(result, mouthShape(after.shape), .5 - .5 * (current.end - time) / radius);
  }
  return result;
}
export function createPngtuberMotion(controls) {
  let open = 0, width = 1, lastTime = null, vowels = { A: 1 };
  const clamp = x => Math.min(1, Math.max(0, x));
  // Consonant 'N' borrows the half-open E/A artwork; a closed mouth keeps the last vowel.
  const VOWEL_OF = { A: { A: 1 }, I: { I: 1 }, U: { U: 1 }, E: { E: 1 }, O: { O: 1 }, N: { E: .6, A: .4 } };
  return {
    reset() { open = 0; width = 1; lastTime = null; vowels = { A: 1 }; }, sample(time, db = -120, active = true, articulation = null, intensity = 1) {
      const dt = lastTime === null ? 0 : Math.max(0, Math.min(.1, time - lastTime)); lastTime = time;
      const loudness = clamp((db - controls.close_threshold_db) / 26);
      const shape = articulation || mouthShape('A');
      const strength = Math.max(.4, Math.min(1.6, intensity));
      const target = active && db > -54 ? clamp(shape.aperture * (.12 + .88 * Math.pow(loudness, 1.25)) * strength) : 0;
      open = active ? open + (target - open) * (1 - Math.exp(-dt / (target > open ? .022 : .045))) : 0;
      const targetWidth = active && target > .005 ? shape.width : 1;
      width += (targetWidth - width) * (1 - Math.exp(-dt / .04));
      const goal = VOWEL_OF[shape.shape];
      if (goal && target > .005) { const k = dt ? 1 - Math.exp(-dt / .035) : 1, next = {}; for (const v of new Set([...Object.keys(vowels), ...Object.keys(goal)])) next[v] = (vowels[v] || 0) + ((goal[v] || 0) - (vowels[v] || 0)) * k; vowels = Object.fromEntries(Object.entries(next).filter(([, w]) => w > .004)); }
      const phase = ((time + 1.7) % (controls.blink_interval_ms / 1000));
      const blink = !active ? 0 : phase < .09 ? phase / .09 : phase < .14 ? 1 : phase < .27 ? 1 - (phase - .14) / .13 : 0;
      return { open: open < .001 ? 0 : open, width, shape: shape.shape, vowels: { ...vowels }, blink: clamp(blink), bob: active ? Math.sin(time * 2 * Math.PI / 4.2) * 1.2 : 0, tilt: active ? Math.sin(time * .7) * .002 : 0 };
    }
  };
}

export async function loadPngtuberImages(asset, images = {}) {
  const entries = [...Object.entries(asset.images), ...Object.entries(asset.vowels || {}).map(([n, v]) => ['vowel_' + n, v]), ...Object.entries(asset.expressions || {}).map(([n, v]) => ['expr_' + n, v]), ...Object.entries(asset.expressions || {}).filter(([, v]) => v.flow).map(([n, v]) => ['exprflow_' + n, v.flow]), ...Object.entries(asset.expressions || {}).filter(([, v]) => v.mouth).map(([n, v]) => ['exprmouth_' + n, v.mouth]), ...Object.entries(asset.idleGlance || {}).map(([i, item]) => ['glance_' + i, item]), ...(asset.motion?.textures || []).map((v, i) => ['motion' + i, v]), ...Object.entries(asset.expressionVowels || {}).flatMap(([n, items]) => Object.entries(items).map(([v, item]) => [`exprvowel_${n}_${v}`, item])), ...Object.entries(asset.upperLipLayers || {}).flatMap(([n, keys]) => keys.map((k, i) => [`upperlip_${n}_${i}`, k]))];
  await Promise.all(entries.map(async ([name, item]) => {
    if (images[name]) return;
    const image = new Image();
    image.src = item.url;
    await image.decode();
    images[name] = image;
  }));
  return images;
}
