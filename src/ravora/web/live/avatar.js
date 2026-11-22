// Render the conversation avatar on the encoder's presentation clock.
import {startAvatar} from '/talk-avatar.js';

window.programAvatar = (async () => {
  const canvas = document.querySelector('#avatar');
  let expression = 'neutral', gesture = 'none', speaking = false;
  const actor = await startAvatar(canvas, () => expression, true, {programClock:true});
  const background = await createImageBitmap(await (await fetch('/avatar-background.jpg')).blob());
  const frame = document.createElement('canvas');
  frame.width = frame.height = canvas.width;
  const g = frame.getContext('2d', {alpha:false});
  return {
    get renderer(){return canvas.parentElement.dataset.renderer;},
    draw(seconds, elapsed, face, motion, pcm){
      expression = face;
      if (pcm !== null) {
        actor.reset();
        const bytes = Uint8Array.from(atob(pcm), c => c.charCodeAt(0));
        const samples = new Int16Array(bytes.buffer);
        actor.pcm(Float32Array.from(samples, x => x/32768));
        actor.finish();
      }
      if (elapsed === null && speaking) actor.stop();
      speaking = elapsed !== null;
      if (pcm !== null || motion !== gesture) actor.motion(motion, seconds*1000);
      gesture = motion;
      const portrait = actor.frame(seconds, elapsed);
      g.drawImage(background, 0, 0, frame.width, frame.height);
      g.drawImage(portrait, 0, 0, frame.width, frame.height);
      return frame.toDataURL('image/jpeg', .93).split(',')[1];
    }
  };
})();
