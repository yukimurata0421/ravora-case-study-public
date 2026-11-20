"""Bind local PNG states and their voice thresholds before browser startup."""
import hashlib
import io
import json
import math
from pathlib import Path

STATES=('idle','talking','blink','talking_blink')
VOWELS=('A','I','U','E','O')
# Expression face patches (scripts/build_expressions.py); names match renderer/expressions.js presets.
EXPRESSIONS=('smile','happy','surprise','thinking','shy','sad','concerned','sleepy','wink')


def pngtuber_binding(root: Path, config: dict):
    section=config['pngtuber']
    manifest=(root/section['manifest']).resolve(strict=True)
    owned=(root/'assets/pngtuber').resolve()
    if not manifest.is_relative_to(owned) or manifest.stat().st_size>262144:
        raise ValueError('PNGTUBER_MANIFEST_OUTSIDE_OWNED_ASSETS_OR_TOO_LARGE')
    document=json.loads(manifest.read_text())
    images=document['images']
    if not {'idle','talking'} <= images.keys() or not images.keys() <= set(STATES):
        raise ValueError('PNGTUBER_IDLE_AND_TALKING_REQUIRED')
    paths={}
    public={}
    pixels=0
    from PIL import Image
    for state in STATES:
        fallback='talking' if state=='talking_blink' else 'idle'
        item=images.get(state,images[fallback])
        path=(root/item['path']).resolve(strict=True)
        if not path.is_relative_to(owned) or path.suffix.lower()!='.png':
            raise ValueError('PNGTUBER_IMAGE_OUTSIDE_OWNED_ASSETS')
        if not 0<path.stat().st_size<=8*1024**2:
            raise ValueError('PNGTUBER_IMAGE_SIZE')
        data=path.read_bytes()
        digest=hashlib.sha256(data).hexdigest()
        if digest!=item['sha256']:
            raise ValueError('PNGTUBER_IMAGE_SHA256_MISMATCH')
        with Image.open(io.BytesIO(data)) as image:
            if image.format!='PNG' or max(image.size)>2048 or getattr(image,'n_frames',1)!=1:
                raise ValueError('PNGTUBER_STATIC_PNG_DIMENSIONS')
            width,height=image.size
            pixels+=width*height
        paths['/pngtuber/'+state+'.png']=path
        public[state]={"url":'/pngtuber/'+state+'.png',"sha256":digest,"width":width,"height":height}
    if pixels>8*1024**2:
        raise ValueError('PNGTUBER_DECODED_PIXEL_BUDGET')
    controls={key:section[key] for key in ('open_threshold_db','close_threshold_db','hold_ms','blink_interval_ms','blink_duration_ms')}
    if not all(isinstance(v,(int,float)) and math.isfinite(v) for v in controls.values()):
        raise ValueError('PNGTUBER_NONFINITE_CONTROL')
    if (not -100<=controls['close_threshold_db']<controls['open_threshold_db']<=0
            or not 0<=controls['hold_ms']<=500 or not 1000<=controls['blink_interval_ms']<=30000
            or not 40<=controls['blink_duration_ms']<=500):
        raise ValueError('PNGTUBER_CONTROL_RANGE')
    regions=_regions(document.get('regions'),public['idle']['width'],public['idle']['height'])
    if regions and any((v['width'],v['height'])!=(public['idle']['width'],public['idle']['height']) for v in public.values()):
        raise ValueError('PNGTUBER_REGIONS_NEED_SAME_CANVAS')
    rig=_rig(document.get('rig'),public['idle']['width'],public['idle']['height'])
    if rig and any((v['width'],v['height'])!=(public['idle']['width'],public['idle']['height']) for v in public.values()):
        raise ValueError('PNGTUBER_RIG_NEEDS_SAME_CANVAS')
    vowels,vowel_paths=_vowels(root,owned,document.get('vowels'),public['idle']['width'],public['idle']['height'])
    paths.update(vowel_paths)
    if rig and any(f['image'].startswith('vowel_') and f['image'][6:] not in (vowels or {}) for f in rig['features']):
        raise ValueError('PNGTUBER_RIG_VOWEL_IMAGE_MISSING')
    expressions,expression_paths=_patches(root,owned,document.get('expressions'),public['idle']['width'],public['idle']['height'],EXPRESSIONS,'expr_','EXPRESSION')
    if expressions and not rig:
        raise ValueError('PNGTUBER_EXPRESSIONS_NEED_RIG')
    if rig and any(f['image'].startswith('exprmouth_') and 'mouth' not in (expressions or {}).get(f['image'][10:],{}) for f in rig['features']):
        raise ValueError('PNGTUBER_RIG_EXPRESSION_MOUTH_MISSING')
    paths.update(expression_paths)
    expression_vowels,ev_paths=_expression_vowels(root,owned,document.get('expression_vowels'),rig,vowels,public['idle']['width'],public['idle']['height'])
    paths.update(ev_paths)
    midline=_face_midline(document.get('face_midline'),public['idle']['width'],public['idle']['height'])
    if midline and not rig:
        raise ValueError('PNGTUBER_FACE_MIDLINE_NEEDS_RIG')
    rig,expression_vowels,face_midline=_face_axis_mouths(rig,expression_vowels,public['idle']['width'],public['idle']['height'],midline)
    rig,expression_vowels=_lip_thickness_mouths(rig,expression_vowels,public['idle']['width'],public['idle']['height'])
    motion,motion_paths=_motion(root,owned,document.get('motion'))
    paths.update(motion_paths)
    glance,glance_paths=_patches(root,owned,document.get('idle_glance'),public['idle']['width'],public['idle']['height'],
        tuple(map(str,range(9))),'glance_','IDLE_GLANCE')
    if glance:
        layout={(tuple(g['offset']),g['width'],g['height']) for g in glance.values()}
        if set(glance)!=set(map(str,range(9))) or len(layout)!=1:
            raise ValueError('PNGTUBER_IDLE_GLANCE_REGISTRATION')
        paths.update(glance_paths)
    reach=document.get('mouth_reach',.7)
    if not isinstance(reach,(int,float)) or not math.isfinite(reach) or not .3<=reach<=1:
        raise ValueError('PNGTUBER_MOUTH_REACH')
    identity={"images":public,"regions":regions} if regions else public
    if rig:identity={"images":public,"regions":regions,"rig":rig}
    if vowels:identity={"images":public,"regions":regions,"rig":rig,"vowels":vowels,"mouth_reach":reach}
    if expressions:identity={**identity,"expressions":expressions}
    if motion:identity={**identity,"motion":motion}
    if expression_vowels:identity={**identity,"expression_vowels":expression_vowels}
    if glance:identity={**identity,"idle_glance":glance}
    if midline:identity={**identity,"face_midline":document['face_midline']}
    digest=hashlib.sha256(json.dumps(identity,sort_keys=True).encode()).hexdigest()
    return paths,{"type":"pngtuber","name":str(document.get('name','PNG avatar'))[:100],"sha256":digest,"regions":regions,"rig":rig,
        "demo":bool(document.get('demo',False)),"images":public,"vowels":vowels,"expressions":expressions,"motion":motion,"expressionVowels":expression_vowels,"idleGlance":glance,"mouthReach":float(reach),"controls":controls,
        "faceMidline":face_midline,"blink_artwork_provided":all(key in images for key in ('blink','talking_blink'))}


def _ellipse(value,width,height):
    keys={'cx','cy','rx','ry'}
    if not isinstance(value,dict) or not keys<=value.keys() or not value.keys()<=keys|{'anchor_y'}:
        raise ValueError('PNGTUBER_REGION_FIELDS')
    if not all(isinstance(value[k],(int,float)) and math.isfinite(value[k]) for k in value):
        raise ValueError('PNGTUBER_REGION_NONFINITE')
    cx,cy,rx,ry=(float(value[k]) for k in ('cx','cy','rx','ry'))
    if not (8<=rx<=width/2 and 8<=ry<=height/2 and rx<=cx<=width-rx and ry<=cy<=height-ry):
        raise ValueError('PNGTUBER_REGION_OUTSIDE_CANVAS')
    out={'cx':cx,'cy':cy,'rx':rx,'ry':ry}
    if 'anchor_y' in value:
        if not cy-ry<=float(value['anchor_y'])<=cy+ry:
            raise ValueError('PNGTUBER_REGION_ANCHOR')
        out['anchor_y']=float(value['anchor_y'])
    return out


def _regions(value,width,height):
    """Optional ellipses (idle-canvas pixels) for compositing in-between mouth/eye states."""
    if value is None:
        return None
    if not isinstance(value,dict) or not value.keys()<={'mouth','eyes'} or 'mouth' not in value:
        raise ValueError('PNGTUBER_REGION_FIELDS')
    eyes=value.get('eyes',[])
    if not isinstance(eyes,list) or len(eyes)>2:
        raise ValueError('PNGTUBER_REGION_FIELDS')
    return {'mouth':_ellipse(value['mouth'],width,height),'eyes':[_ellipse(e,width,height) for e in eyes]}


def _rig(value,width,height):
    """Validate a bounded correspondence mesh, including folds BETWEEN endpoints."""
    if value is None:return None
    if not isinstance(value,dict) or set(value)!={'version','features'} or value['version']!=1:
        raise ValueError('PNGTUBER_RIG_SCHEMA')
    features=value['features']
    if not isinstance(features,list) or not 1<=len(features)<=12:raise ValueError('PNGTUBER_RIG_FEATURES')
    for f in features:
        if not isinstance(f,dict) or set(f)!={'kind','image','columns','rows','source','target','interior_rows','region'}:
            raise ValueError('PNGTUBER_RIG_FIELDS')
        if (f['kind'],f['image']) not in [('mouth','talking'),('eye','blink'),*[('mouth','vowel_'+v) for v in VOWELS],*[('mouth','exprmouth_'+e) for e in EXPRESSIONS]]:raise ValueError('PNGTUBER_RIG_SOURCE')
        cols,rows=f['columns'],f['rows']
        if type(cols)!=int or type(rows)!=int or not (3<=cols<=32 and 4<=rows<=16):raise ValueError('PNGTUBER_RIG_SIZE')
        if not isinstance(f['interior_rows'],list) or len(f['interior_rows'])!=2 or any(type(r)!=int or not 1<=r<rows-1 for r in f['interior_rows']):raise ValueError('PNGTUBER_RIG_INTERIOR')
        if f['interior_rows'][1]!=f['interior_rows'][0]+1:raise ValueError('PNGTUBER_RIG_INTERIOR')
        if not isinstance(f['region'],list) or len(f['region'])!=4:raise ValueError('PNGTUBER_RIG_REGION')
        _ellipse(dict(zip(('cx','cy','rx','ry'),f['region'])),width,height)
        for key in ['source','target']:
            a=f[key]
            if not isinstance(a,list) or len(a)!=cols*rows*2 or any(type(v) not in (float,int) or not math.isfinite(v) or not 0<=v<=(width if i%2==0 else height) for i,v in enumerate(a)):
                raise ValueError('PNGTUBER_RIG_COORDINATES')
        for i in range(cols*rows):
            if i//cols in (0,rows-1) or i%cols in (0,cols-1):
                if f['source'][2*i:2*i+2]!=f['target'][2*i:2*i+2]:raise ValueError('PNGTUBER_RIG_BOUNDARY_MOVES')
        def cross(a,b):return a[0]*b[1]-a[1]*b[0]
        for row in range(rows-1):
            for col in range(cols-1):
                i=row*cols+col
                for ids in [(i,i+1,i+cols),(i+1,i+cols+1,i+cols)]:
                    pairs=[]
                    for key in ('source','target'):
                        p=[f[key][2*k:2*k+2] for k in ids]
                        pairs.append(([p[1][j]-p[0][j] for j in (0,1)],[p[2][j]-p[0][j] for j in (0,1)]))
                    a,b=pairs[0];da=[pairs[1][0][j]-a[j] for j in (0,1)];db=[pairs[1][1][j]-b[j] for j in (0,1)]
                    q0,q1,q2=cross(a,b),cross(da,b)+cross(a,db),cross(da,db)
                    points=[0,1]
                    if q2>0 and 0<-q1/(2*q2)<1:points.append(-q1/(2*q2))
                    if min(q0+q1*t+q2*t*t for t in points)<.001:raise ValueError('PNGTUBER_RIG_FOLD_OR_DEGENERATE')
    mouths=[f['image'] for f in features if f['kind']=='mouth']
    if len(mouths)!=len(set(mouths)) or sum(f['kind']=='eye' for f in features)>2:raise ValueError('PNGTUBER_RIG_FEATURES')
    return value


def _vowels(root,owned,value,width,height):
    """Optional cropped mouth textures, one per vowel, placed at `offset` on the idle canvas."""
    return _patches(root,owned,value,width,height,VOWELS,'vowel_','VOWEL')


def _patches(root,owned,value,width,height,names,prefix,label):
    """Cropped PNG patches keyed by name, placed at `offset` on the idle canvas."""
    if value is None:
        return None,{}
    if not isinstance(value,dict) or not value or not set(value)<=set(names):
        raise ValueError(f'PNGTUBER_{label}_FIELDS')
    from PIL import Image
    public,paths=({},{})
    for name,item in value.items():
        allowed={'path','sha256','offset','source_sha256'}|({'flow','mouth','detail_band'} if prefix=='expr_' else set())
        if prefix=='expr_' and name=='thinking':
            allowed.add('authored_mouth_detail')
        if not isinstance(item,dict) or not {'path','sha256','offset'}<=set(item) or not set(item)<=allowed:
            raise ValueError(f'PNGTUBER_{label}_FIELDS')
        path=(root/item['path']).resolve(strict=True)
        if not path.is_relative_to(owned) or path.suffix.lower()!='.png' or not 0<path.stat().st_size<=2*1024**2:
            raise ValueError(f'PNGTUBER_{label}_IMAGE')
        data=path.read_bytes()
        if hashlib.sha256(data).hexdigest()!=item['sha256']:
            raise ValueError('PNGTUBER_IMAGE_SHA256_MISMATCH')
        with Image.open(io.BytesIO(data)) as image:
            if image.format!='PNG' or max(image.size)>512 or getattr(image,'n_frames',1)!=1:
                raise ValueError(f'PNGTUBER_{label}_IMAGE')
            w,h=image.size
        ox,oy=item['offset'] if isinstance(item['offset'],list) and len(item['offset'])==2 else (None,None)
        if type(ox)!=int or type(oy)!=int or not (0<=ox and 0<=oy and ox+w<=width and oy+h<=height):
            raise ValueError(f'PNGTUBER_{label}_OFFSET')
        url='/pngtuber/'+prefix+name+'.png'
        paths[url]=path
        public[name]={"url":url,"sha256":item['sha256'],"offset":[ox,oy],"width":w,"height":h}
        if 'authored_mouth_detail' in item:
            if type(item['authored_mouth_detail']) is not bool:
                raise ValueError('PNGTUBER_EXPRESSION_AUTHORED_MOUTH_DETAIL')
            public[name]['authoredMouthDetail']=item['authored_mouth_detail']
        if 'detail_band' in item:
            # Rows [full_y, zero_y]: above full_y the expression only moves the neutral texture
            # (its own drawing is not used), fading to the drawn expression by zero_y.
            band=item['detail_band']
            if not (isinstance(band,list) and len(band)==2 and all(type(v) is int for v in band) and 0<=band[0]<band[1]<=height):
                raise ValueError(f'PNGTUBER_{label}_DETAIL_BAND')
            public[name]["detailBand"]=band
        if 'flow' in item:
            # Morph field for the patch (scripts/build_expressions.py): same size, opaque RGBA.
            flow=item['flow']
            if not isinstance(flow,dict) or set(flow)!={'path','sha256'}:
                raise ValueError(f'PNGTUBER_{label}_FLOW')
            flow_path=(root/flow['path']).resolve(strict=True)
            if not flow_path.is_relative_to(owned) or flow_path.suffix.lower()!='.png' or not 0<flow_path.stat().st_size<=2*1024**2:
                raise ValueError(f'PNGTUBER_{label}_FLOW')
            flow_data=flow_path.read_bytes()
            if hashlib.sha256(flow_data).hexdigest()!=flow['sha256']:
                raise ValueError('PNGTUBER_IMAGE_SHA256_MISMATCH')
            with Image.open(io.BytesIO(flow_data)) as image:
                if image.format!='PNG' or image.size!=(w,h) or image.mode!='RGBA' or getattr(image,'n_frames',1)!=1:
                    raise ValueError(f'PNGTUBER_{label}_FLOW')
            flow_url='/pngtuber/'+prefix+'flow_'+name+'.png'
            paths[flow_url]=flow_path
            public[name]["flow"]={"url":flow_url,"sha256":flow['sha256']}
        if 'mouth' in item:
            # Mouth crop for expressions whose mouth opens; drawn through the exprmouth_<name> rig mesh.
            mouth,mouth_paths=_patches(root,owned,{name:item['mouth']},width,height,names,prefix+'mouth_',label+'_MOUTH')
            paths.update(mouth_paths)
            public[name]["mouth"]=mouth[name]
    return public,paths


MOTION_FIELDS=('01_head_tilt_left','02_head_tilt_right','03_head_turn_left','04_head_turn_right','05_gaze_left','06_gaze_right','07_gaze_up','08_gaze_down')


def _motion(root,owned,value):
    """Optional head/gaze displacement fields (scripts/build_motion.py): 4 opaque RGBA PNGs, same size."""
    if value is None:
        return None,{}
    if not isinstance(value,dict) or set(value)!={'scale','fields','textures'} or value['fields']!=list(MOTION_FIELDS) \
            or type(value['scale'])!=int or not 1<=value['scale']<=8 or not isinstance(value['textures'],list) or len(value['textures'])!=4:
        raise ValueError('PNGTUBER_MOTION_FIELDS')
    from PIL import Image
    public,paths,size=[],{},None
    for i,item in enumerate(value['textures']):
        if not isinstance(item,dict) or set(item)!={'path','sha256'}:
            raise ValueError('PNGTUBER_MOTION_FIELDS')
        path=(root/item['path']).resolve(strict=True)
        if not path.is_relative_to(owned) or path.suffix.lower()!='.png' or not 0<path.stat().st_size<=2*1024**2:
            raise ValueError('PNGTUBER_MOTION_IMAGE')
        data=path.read_bytes()
        if hashlib.sha256(data).hexdigest()!=item['sha256']:
            raise ValueError('PNGTUBER_IMAGE_SHA256_MISMATCH')
        with Image.open(io.BytesIO(data)) as image:
            if image.format!='PNG' or image.mode!='RGBA' or max(image.size)>512 or (size and image.size!=size):
                raise ValueError('PNGTUBER_MOTION_IMAGE')
            size=image.size
        url=f'/pngtuber/motion{i}.png'
        paths[url]=path
        public.append({"url":url,"sha256":item['sha256']})
    return {"scale":value['scale'],"fields":list(MOTION_FIELDS),"textures":public},paths


def _corner_angle(points,cols):
    l,r=points[2*(2*cols+1):2*(2*cols+1)+2],points[2*(2*cols+cols-2):2*(2*cols+cols-2)+2]
    return math.atan2(r[1]-l[1],r[0]-l[0])


MIDLINE_POINTS=('nose','upper_lip','lower_lip','chin')


def _face_midline(value,width,height):
    """The portrait's face midline (scripts/measure_face_midline.py): x = a + b*y.

    Measured as the line through the nose and the upper lip whose extension carries the lower lip
    and the chin tip. Double check: the four landmarks come from independent cues and must agree
    (each within 1.5 px of the fitted line), so a mis-measured point cannot tilt the mouth."""
    if value is None:return None
    if not isinstance(value,dict) or set(value)!=set(MIDLINE_POINTS):raise ValueError('PNGTUBER_FACE_MIDLINE_FIELDS')
    pts=[value[k] for k in MIDLINE_POINTS]
    if any(not isinstance(q,list) or len(q)!=2 or any(type(v) not in (int,float) or not math.isfinite(v) for v in q)
           or not (0<=q[0]<=width and 0<=q[1]<=height) for q in pts):raise ValueError('PNGTUBER_FACE_MIDLINE_FIELDS')
    if any(pts[i][1]>=pts[i+1][1] for i in range(3)) or pts[3][1]-pts[0][1]<20:raise ValueError('PNGTUBER_FACE_MIDLINE_ORDER')
    my=sum(q[1] for q in pts)/4;mx=sum(q[0] for q in pts)/4
    b=sum((q[1]-my)*(q[0]-mx) for q in pts)/sum((q[1]-my)**2 for q in pts);a=mx-b*my
    if abs(math.atan(b))>.35:raise ValueError('PNGTUBER_FACE_MIDLINE_ANGLE')
    if any(abs(q[0]-(a+b*q[1]))>1.5 for q in pts):raise ValueError('PNGTUBER_FACE_MIDLINE_DISAGREES')
    return a,b


def _midline_column(points,row,cols,line):
    """Fractional column where one mesh row crosses the midline (None if it does not)."""
    a,b=line;d=[points[2*(row*cols+c)]-(a+b*points[2*(row*cols+c)+1]) for c in range(cols)]
    for c in range(1,cols-2):
        if d[c]<=0<=d[c+1] or d[c]>=0>=d[c+1]:
            return c+(d[c]/(d[c]-d[c+1]) if d[c]!=d[c+1] else 0.)
    return None


def _align_to_face(source,target,cols,rows,line,strength=1.):
    """Re-express one measured mouth pose along the portrait's own face midline.

    The rig is measured in screen coordinates: the chin row only moves in y at its x, the lip
    columns are spread between the corners, and the generated open mouths are drawn closer to
    level than the tilted portrait (vowel A +3.9 deg, U +5.5, O +5.0). The portrait is also turned
    slightly, so the midline (nose, upper lip, lower lip, chin: 8.2 deg) is neither vertical nor
    at the mesh centre (the corner midpoint lies 7 px to the right of it) and expressions widen
    the corners unevenly (happy A: left -26 px, right +16 px). Restore the rest tilt of the corner
    line (a vertical shear, so rows stacked 1 px apart at a corner keep their order), then shift
    each row sideways so that the lip or chin point that sat on the midline at rest is on the
    midline again: the lower lip and chin drop along the line from the nose and upper lip. Fixed
    border rows and columns stay where they are."""
    alpha=max(-.12,min(.12,_corner_angle(target,cols)-_corner_angle(source,cols)))*strength
    l,r=target[2*(2*cols+1):2*(2*cols+1)+2],target[2*(2*cols+cols-2):2*(2*cols+cols-2)+2]
    cx=(l[0]+r[0])/2;level=math.tan(-alpha);a,b=line
    out=list(target)
    for row in range(1,rows-1):
        for col in range(1,cols-1):
            i=2*(row*cols+col);out[i+1]=target[i+1]+(target[i]-cx)*level
    # The corners are one point drawn by rows stacked 1-2 px apart: every row moves them by the
    # same (mean) shift, and its own shift applies at the midline, eased in between.
    shifts={}
    for row in range(1,rows-1):
        u=_midline_column(source,row,cols,line)
        if u is None:continue
        c=int(u);f=u-c;i,j=2*(row*cols+c),2*(row*cols+c+1)
        x,y=out[i]*(1-f)+out[j]*f,out[i+1]*(1-f)+out[j+1]*f
        shifts[row]=(u,(a+b*y-x)*strength)
    if shifts:
        common=sum(v for _,v in shifts.values())/len(shifts)
        for row in range(1,rows-1):
            u,own=shifts.get(row,(None,common))
            for col in range(1,cols-1):
                w=0. if u is None else max(0.,1-abs(col-u)/(u-1 if col<u else cols-2-u))
                out[2*(row*cols+col)]+=common+(own-common)*w*w*(3-2*w)
    return [round(v,2) if (k//2)//cols not in (0,rows-1) and (k//2)%cols not in (0,cols-1) else v for k,v in enumerate(out)]


def _aligned_or_less(feature,target,line,width,height):
    """Largest correction (1, 1/2, 1/4) that keeps the mesh fold-free; else the measured pose."""
    for strength in (1.,.5,.25):
        t=_align_to_face(feature['source'],target,feature['columns'],feature['rows'],line,strength)
        try:
            _rig({'version':1,'features':[{**feature,'target':t}]},width,height);return t
        except ValueError:continue
    return target


def _face_axis_mouths(rig,expression_vowels,width,height,midline=None):
    """Mouth poses aligned to the measured face midline. Without one, a midline is estimated
    through the rest mouth centre, square to the eye line and corner line."""
    if not rig:return rig,expression_vowels,None
    mouths=[f for f in rig['features'] if f['kind']=='mouth' and f['rows']>=7 and f['columns']>=5]
    if not mouths:return rig,expression_vowels,None
    if midline is None:
        eyes=sorted(f['region'] for f in rig['features'] if f['kind']=='eye')
        if len(eyes)!=2:return rig,expression_vowels,None
        src,cols=mouths[0]['source'],mouths[0]['columns']
        tilt=(math.atan2(eyes[1][1]-eyes[0][1],eyes[1][0]-eyes[0][0])+_corner_angle(src,cols))/2
        if abs(tilt)>.35:return rig,expression_vowels,None
        l,r=src[2*(2*cols+1):2*(2*cols+1)+2],src[2*(2*cols+cols-2):2*(2*cols+cols-2)+2]
        b=-math.tan(tilt);midline=((l[0]+r[0])/2-b*(l[1]+r[1])/2,b)
    features=[{**f,'target':_aligned_or_less(f,f['target'],midline,width,height)} if f in mouths else f for f in rig['features']]
    rig=_rig({**rig,'features':features},width,height)
    if expression_vowels:
        neutral={f['image'][6:]:f for f in features if f['image'].startswith('vowel_')}
        aligned={}
        for name,items in expression_vowels.items():
            aligned[name]={}
            for v,item in items.items():
                base=neutral[v]
                t2=_aligned_or_less(base,item['target2'],midline,width,height) if base['rows']>=7 and base['columns']>=5 else item['target2']
                aligned[name][v]={**item,'target2':t2}
        expression_vowels=aligned
    a,b=midline
    return rig,expression_vowels,{"x0":round(a,3),"slope":round(b,5),"angle":round(math.atan(b),5)}


LIP_THICKNESS=(.9,1.1)
LIP_THICKNESS_ROUNDED=(.9,1.3)   # U and O push the lips out: more of them shows


def lip_thickness_ratio(source,target,cols):
    """Upper and lower lip thickness of a 7-row mouth pose over its rest thickness, on the five
    centre columns (rows 1-2: upper lip, rows 3-4: lower lip)."""
    mid=range(cols//2-2,cols//2+3)
    y=lambda p,r,c:p[2*(r*cols+c)+1]
    up=sum(y(target,2,c)-y(target,1,c) for c in mid)/max(1e-6,sum(y(source,2,c)-y(source,1,c) for c in mid))
    lo=sum(y(target,4,c)-y(target,3,c) for c in mid)/max(1e-6,sum(y(source,4,c)-y(source,3,c) for c in mid))
    return up,lo


def _lip_thickness(feature,target,vowel,width,height):
    """Keep each pose's lips within a band of their rest thickness.

    The generated vowel artworks disagree on lip thickness: the smiling lower lip is 32 px in A,
    18 in I and 24 in E against 23.5 at rest, so in conversation the lower lip swelled and thinned
    with every syllable (and the closed smile between them keeps the rest thickness). Only the
    outer lip edges move (row 1: upper lip's top, row 4: lower lip's bottom); the seams, corners
    and chin stay. The artwork is registered to these rows, so it is drawn at that thickness."""
    cols=feature['columns'];lo_b,hi_b=LIP_THICKNESS_ROUNDED if vowel in ('U','O') else LIP_THICKNESS
    up,lo=lip_thickness_ratio(feature['source'],target,cols)
    su=min(hi_b,max(lo_b,up))/up if up>0 else 1.;sl=min(hi_b,max(lo_b,lo))/lo if lo>0 else 1.
    if abs(su-1)<1e-3 and abs(sl-1)<1e-3:return target
    for strength in (1.,.5,.25):
        out=list(target)
        for c in range(1,cols-1):
            i1,i2,i3,i4=(2*(r*cols+c)+1 for r in (1,2,3,4))
            out[i1]=round(target[i2]-(target[i2]-target[i1])*(1+(su-1)*strength),2)
            out[i4]=round(target[i3]+(target[i4]-target[i3])*(1+(sl-1)*strength),2)
        try:
            _rig({'version':1,'features':[{**feature,'target':out}]},width,height);return out
        except ValueError:continue
    return target


def _lip_thickness_mouths(rig,expression_vowels,width,height):
    if not rig:return rig,expression_vowels
    vowel_of=lambda f:f['image'][6:] if f['image'].startswith('vowel_') else None
    features=[{**f,'target':_lip_thickness(f,f['target'],vowel_of(f),width,height)}
              if f['kind']=='mouth' and f['rows']==7 and f['columns']>=5 else f for f in rig['features']]
    rig=_rig({**rig,'features':features},width,height)
    if expression_vowels:
        neutral={f['image'][6:]:f for f in features if f['image'].startswith('vowel_')}
        expression_vowels={name:{v:{**item,'target2':_lip_thickness(neutral[v],item['target2'],v,width,height)
                                     if neutral[v]['rows']==7 else item['target2']}
                                  for v,item in items.items()} for name,items in expression_vowels.items()}
    return rig,expression_vowels


def _expression_vowels(root,owned,value,rig,vowels,width,height):
    """Per-expression vowel mouths (scripts/build_expression_vowels.py): a crop per expression and
    vowel plus a second target for the neutral vowel's mesh (same vertices, no folds)."""
    if value is None:
        return None,{}
    if not rig or not vowels or not isinstance(value,dict) or not set(value)<=set(EXPRESSIONS):
        raise ValueError('PNGTUBER_EXPRESSION_VOWEL_FIELDS')
    neutral={f['image'][6:]:f for f in rig['features'] if f['image'].startswith('vowel_')}
    public,paths={},{}
    for name,items in value.items():
        if not isinstance(items,dict) or not set(items)<=set(VOWELS) or not set(items)<=set(neutral):
            raise ValueError('PNGTUBER_EXPRESSION_VOWEL_FIELDS')
        public[name]={}
        for v,item in items.items():
            if not isinstance(item,dict) or 'target2' not in item:
                raise ValueError('PNGTUBER_EXPRESSION_VOWEL_FIELDS')
            crop={k:item[k] for k in item if k!='target2'}
            got,crop_paths=_patches(root,owned,{v:crop},width,height,VOWELS,f'exprvowel_{name}_','EXPRESSION_VOWEL')
            base=neutral[v]
            _rig({'version':1,'features':[{**base,'target':item['target2']}]},width,height)
            paths.update(crop_paths)
            public[name][v]={**got[v],"target2":item['target2']}
    return public,paths
