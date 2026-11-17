export function sampleMouthPlan(plan,time){
  const none={amount:0,vowels:null,shape:'closed'};
  if(!Array.isArray(plan)||!plan.length||!Number.isFinite(time))return none;
  const hold=k=>{const d=k.end-k.start,m=k.shape==='closed'?.2:.3;return[k.start+d*m,k.end-d*m];};
  let low=0,high=plan.length;
  while(low<high){const mid=(low+high)>>1;if(hold(plan[mid])[1]<time)low=mid+1;else high=mid;}
  const next=plan[low],prev=plan[low-1];
  const pose=k=>({amount:k.shape==='closed'?0:k.amount,shape:k.shape});
  if(!next){if(!prev)return none;const p=pose(prev),fade=Math.min(1,Math.max(0,(time-hold(prev)[1])/.12));return{amount:p.amount*(1-fade),vowels:p.shape==='closed'?null:{[p.shape]:1},shape:p.shape};}
  const [a]=hold(next);
  if(time>=a||!prev){const p=pose(next);if(!prev&&time<a){const k=Math.min(1,Math.max(0,1-(a-time)/.12));return{amount:p.amount*k,vowels:p.shape==='closed'?null:{[p.shape]:1},shape:p.shape};}return{amount:p.amount,vowels:p.shape==='closed'?null:{[p.shape]:1},shape:p.shape};}
  const b=hold(prev)[1],u0=Math.min(1,Math.max(0,(time-b)/Math.max(1e-3,a-b))),u=u0*u0*(3-2*u0);
  const p=pose(prev),n=pose(next);const amount=p.amount+(n.amount-p.amount)*u;
  let vowels;
  if(p.shape==='closed'&&n.shape==='closed')vowels=null;
  else if(p.shape==='closed')vowels={[n.shape]:1};
  else if(n.shape==='closed')vowels={[p.shape]:1};
  else if(p.shape===n.shape)vowels={[p.shape]:1};
  else vowels={[p.shape]:1-u,[n.shape]:u};
  return{amount,vowels,shape:u<.5?p.shape:n.shape};
}

// Interleaved mesh vertices have 11 values. The two registered target
// positions begin at offsets 2 and 6. The renderer projects both textures
// through this shared shape so their lip contours do not separate.
export function sharedMouthTargets(layers) {
  if (layers.length < 2) return null;
  const size = layers[0].vertices.length;
  if (!size || size % 11 || layers.some(layer => layer.vertices.length !== size)) return null;
  const total = layers.reduce((sum, layer) => sum + layer.weight, 0);
  if (!Number.isFinite(total) || total <= 0) return null;
  const shared = new Float32Array(size / 11 * 2);
  for (const {vertices, weight, expressionMix = 0} of layers) {
    for (let i = 0; i < size / 11; i++) {
      const j = i * 11;
      shared[i * 2] += weight / total * (vertices[j + 2] + (vertices[j + 6] - vertices[j + 2]) * expressionMix);
      shared[i * 2 + 1] += weight / total * (vertices[j + 3] + (vertices[j + 7] - vertices[j + 3]) * expressionMix);
    }
  }
  return shared;
}
