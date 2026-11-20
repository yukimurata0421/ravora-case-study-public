"""Synthetic replay and an explicitly offline console; no host deployment hooks."""
import argparse
import asyncio
from dataclasses import replace
import json
from pathlib import Path
import socket
import tempfile
import tomllib
import threading
import time
from unittest.mock import patch
import uuid
import wave

from ravora.comments import Comment,CommentInbox
from ravora.context_prompt import context_text
from ravora.conversation import MemoryStore,SpeechController
from ravora.fast_voice import FastVoiceSession
from ravora.fast_voice_server import FastVoiceServer,MockVoiceClients
from ravora.providers import Transport
from ravora.settings import ProviderSettings
from ravora.relationship_demo import relationship_replay
from ravora.media_timing import media_timing_replay


def offline_settings():
    # Deliberately independent of inherited environment variables and dotenv.
    return ProviderSettings(mode='offline',api_enabled=False,stt_api_enabled=False,
        memory_api_enabled=False,youtube_read_enabled=False,memory_extractor='local_rules',
        memory_turns=0,developer_id='case-study-developer').validate()


class StudyClients(MockVoiceClients):
    def __init__(self):self.context=[];self.audio_gate=asyncio.Event();self.text_finished=False
    async def generate(self,text,*,context=(),**kwargs):
        self.context=list(context);self.text_finished=False;self.audio_gate=asyncio.Event()
        yield 'Here is a brief coffee explanation. '
        # The test releases this only after PCM has reached the local sink.
        await asyncio.wait_for(self.audio_gate.wait(),3)
        yield 'Change one brewing variable at a time.'
        self.text_finished=True


class SampleText:
    def __init__(self,reply):self.reply=reply;self.context=[];self.memories=[]
    def set_context(self,context):self.context=list(context)
    def set_memories(self,memories):self.memories=list(memories)
    def stream(self,comment,history):yield self.reply


class SampleReaction:
    def classify(self,comment):return {'reaction':'neutral','source':'synthetic'}


class SampleAudio:
    def create(self,text,path):
        # A short waveform fixture, not speech and not a model output.
        with wave.open(str(path),'wb') as audio:
            audio.setnchannels(1);audio.setsampwidth(2);audio.setframerate(24000);audio.writeframes(b'\x00\x08'*2400)
        return {'duration':.1,'source':'synthetic-waveform','visemes':[]}


def comment_reply(store,settings,root,index):
    text=f'How should I prepare coffee sample {index}?'
    reply=f'For coffee sample {index}, adjust the grind before changing temperature.'
    comment=Comment('youtube','fictional-chat',str(index),'fictional-viewer','Fictional viewer',text,time.time())
    inbox=CommentInbox(store);assert inbox.put(comment)=='queued';inbox.take();inbox.claimed(comment.receipt)
    turn=uuid.uuid4().hex;assert inbox.bind(comment.receipt,turn)
    controller=SpeechController(settings,store,root,providers=(SampleText(reply),SampleReaction(),SampleAudio()))
    events=[]
    try:
        controller.run(text,comment.viewer,turn,events.append,threading.Event())
        assert events[-1]['kind']=='complete'
        for event in events:
            if event['kind']=='audio':store.chunk_claimed(turn,event['index'])
        assert store.played(turn,scope='offline',memory_enabled=False)
        inbox.finish(comment.receipt,'played')
    finally:controller.memory_worker.close();inbox.close()
    return comment.viewer


async def replay():
    settings=offline_settings();clients=StudyClients();events=[];audio_count=0;early_pcm=False;arrived=asyncio.Event()
    with tempfile.TemporaryDirectory(prefix='ravora-case-study-') as directory, \
            patch.object(Transport,'open',side_effect=AssertionError('LIVE_PROVIDER_FORBIDDEN')), \
            patch.object(socket,'create_connection',side_effect=AssertionError('NETWORK_FORBIDDEN')):
        root=Path(directory);store=MemoryStore(root/'conversation.sqlite3')
        async def send(event):events.append(event)
        async def pcm(token,data):
            nonlocal audio_count,early_pcm
            audio_count+=1;early_pcm=early_pcm or not clients.text_finished
            clients.audio_gate.set();arrived.set()
        session=FastVoiceSession(settings,store,root,clients,send,pcm,mock=True)
        try:
            await session.start_text(uuid.uuid4().hex,'I prefer brief coffee explanations. My fictional draft is Cedar.')
            await asyncio.wait_for(session.task,6);assert session.phase=='generated'
            developer_turn=session.token['turn_id'];assert session.played(session.token,session.expected_frames)
            for index in range(5):viewer=comment_reply(store,settings,root,index)
            public_before=store.recall_context(viewer,'coffee',scope='offline',developer_id=settings.developer_id)
            assert not any(entry['tier']==1 for entry in public_before)
            # This approval concerns only the explicitly fictional sample above.
            review=store.share_turn_context(developer_turn,'Developer prefers brief coffee explanations.',
                developer_id=settings.developer_id,reviewed_non_sensitive=True)
            public_after=store.recall_context(viewer,'coffee',scope='offline',developer_id=settings.developer_id)
            assert public_after[0]['tier']==1 and 'Cedar' not in context_text(public_after)
            await session.start_text(uuid.uuid4().hex,'Please explain coffee again.')
            await asyncio.wait_for(session.task,6);assert session.phase=='generated'
            tiers=[e['tier'] for e in clients.context];assert set(tiers)=={1,2,3} and tiers==sorted(tiers)
            assert len(context_text(clients.context))<=settings.context_max_chars
            assert session.played(session.token,session.expected_frames)
            assert store.revoke_context_share(review['memory_id'])==1
            assert not any(e['tier']==1 for e in store.recall_context(viewer,'coffee',scope='offline',developer_id=settings.developer_id))
            arrived.clear();await session.start_text(uuid.uuid4().hex,'One more fictional coffee question.')
            await asyncio.wait_for(arrived.wait(),3);stale=dict(session.token);await session.cancel()
            count=audio_count;await asyncio.sleep(.06);assert count==audio_count and not session.current(stale)
            with store.connect() as db:
                assert db.execute('SELECT played,status FROM conversation_turns WHERE turn_id=?',(stale['turn_id'],)).fetchone()==(0,'cancelled')
                attempts=db.execute('SELECT COALESCE(SUM(attempted),0) FROM provider_calls').fetchone()[0]
                assert attempts==0
                context_rows=db.execute('SELECT COUNT(*) FROM context_retrievals WHERE context_prepared=1').fetchone()[0]
                assert context_rows>0
            result={'status':'PASS_CASE_STUDY_REPLAY','synthetic_data':True,'schema_version':store.status()['user_version'],
                'tier_order':tiers,'private_developer_context_excluded_from_public_reply':True,
                'reviewed_summary_shared_without_private_transcript':True,'sharing_revocation_verified':True,
                'pcm_before_text_complete':early_pcm,'stale_audio_fenced_after_cancel':True,
                'context_rows_prepared':context_rows,'external_api_calls':attempts,
                'playback_acknowledgement':'simulated','physical_audio_or_avatar_verified':False,
                'temporary_database_removed_on_exit':True}
        finally:await session.close()
    return result


async def early_reaction_replay():
    """Exercise real controller ordering with synthetic, low-stability updates."""
    settings=offline_settings();prefix=asyncio.Event();reaction=asyncio.Event()
    events=[];final_inputs=[];released=False;reaction_while_listening=False
    final_text='I am really happy about this fictional project.'
    class Clients(MockVoiceClients):
        async def transcribe_frames(self,frames,emit,**kwargs):
            size=0
            async for data in frames:
                size+=len(data)
                value='I am really' if size==6400 else 'I am really happy' if size==12800 else final_text
                await emit({'kind':'stt_partial','text':value,'stability':.01,'segment_final':False})
                prefix.set()
            await emit({'kind':'input_complete','input_seconds':size/32000})
            return final_text,{'mock':True}
        async def generate(self,text,**kwargs):
            assert released,'GENERATION_STARTED_BEFORE_INPUT_END'
            final_inputs.append(text)
            async for delta in super().generate(text,**kwargs):yield delta
    with tempfile.TemporaryDirectory(prefix='ravora-early-replay-') as directory, \
            patch.object(Transport,'open',side_effect=AssertionError('LIVE_PROVIDER_FORBIDDEN')), \
            patch.object(socket,'create_connection',side_effect=AssertionError('NETWORK_FORBIDDEN')):
        root=Path(directory);store=MemoryStore(root/'conversation.sqlite3')
        async def send(value):
            nonlocal reaction_while_listening
            events.append(value)
            if value['kind']=='reaction' and value['phase']=='partial':
                reaction_while_listening=session.phase=='listening' and not released
                reaction.set()
        async def pcm(token,data):pass
        session=FastVoiceSession(settings,store,root,Clients(),send,pcm,mock=True)
        try:
            identifier=uuid.uuid4().hex;await session.start_audio(identifier,'en-US')
            await session.audio_chunk(identifier,b'\x00\x10'*3200);await asyncio.wait_for(prefix.wait(),2)
            # This advances the state-machine fixture; it is not a latency benchmark.
            await asyncio.sleep(settings.early_reaction_debounce_ms/1000+.03)
            await session.audio_chunk(identifier,b'\x00\x10'*3200)
            await asyncio.wait_for(reaction.wait(),2)
            assert reaction_while_listening and not final_inputs
            with store.connect() as db:
                assert db.execute('SELECT COUNT(*) FROM source_events').fetchone()[0]==0
                assert db.execute('SELECT COUNT(*) FROM memory_jobs').fetchone()[0]==0
            await session.audio_chunk(identifier,b'\x00\x10'*3200)
            released=True;await session.end_audio(identifier);await asyncio.wait_for(session.task,6)
            await asyncio.wait_for(session.early.task,2)
            assert final_inputs==[final_text] and session.phase=='generated'
            updates=[event for event in events if event['kind']=='reaction']
            assert [(e['reaction_sequence'],e['reaction'],e['transition']) for e in updates]==[
                (1,'joy','switch'),(2,'neutral','return_neutral')]
            assert session.played(session.token,session.expected_frames)
            with store.connect() as db:
                assert db.execute('SELECT COUNT(*) FROM early_reaction_calls').fetchone()[0]==1
                assert db.execute("SELECT COUNT(*) FROM provider_calls WHERE provider='jev'").fetchone()[0]==1
                assert db.execute('SELECT COUNT(*) FROM input_reaction_claims').fetchone()[0]==1
                assert db.execute('SELECT stage FROM reaction_stage_claims ORDER BY stage').fetchall()==[('final',),('partial',)]
                assert db.execute('SELECT text FROM source_events').fetchall()==[(final_text,)]
                attempts=sum(db.execute(sql).fetchone()[0] for sql in (
                    'SELECT COALESCE(SUM(attempted),0) FROM provider_calls',
                    'SELECT COALESCE(SUM(attempted),0) FROM early_reaction_calls',
                    'SELECT COALESCE(SUM(request_attempted),0) FROM developer_inputs'))
                assert attempts==0
            result={'status':'PASS_EARLY_REACTION_REPLAY','synthetic_data':True,
                'low_stability_prefix_used':True,'reaction_before_input_end':reaction_while_listening,
                'llm_received_final_only':True,'partial_text_absent_from_sources':True,
                'reaction_slots_used':2,'mock_reaction_calls':2,'external_api_calls':attempts,
                'reactions':[{'phase':e['phase'],'reaction':e['reaction'],'transition':e['transition']} for e in updates],
                'playback_acknowledgement':'simulated','latency_measurement':False,
                'physical_audio_or_avatar_verified':False,'temporary_database_removed_on_exit':True}
        finally:await session.close()
    return result


async def console(port,seconds,*,route=None,avatar_binding=None):
    route=route or {}
    with tempfile.TemporaryDirectory(prefix='ravora-console-') as directory:
        root=Path(directory);store=MemoryStore(root/'conversation.sqlite3')
        server=await FastVoiceServer(offline_settings(),store,root,mode='mock',port=port,
            default_input=route.get('input_source','windows'),dell_device=route.get('dell_device'),
            default_end_mode=route.get('end_mode','manual'),end_silence_ms=route.get('end_silence_ms',800),
            avatar_binding=avatar_binding).start()
        print('Offline simulation: canned transcription, test tones, no cloud calls.',flush=True)
        print('Open: '+server.origin+'/#token='+server.secret,flush=True)
        print('Session expires in '+str(seconds)+' seconds; temporary text and audio are removed at exit.',flush=True)
        try:await asyncio.sleep(seconds)
        finally:await server.close()


def main():
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('action',choices=('replay','early-reaction','relationships','media-timing','serve'),nargs='?',default='replay')
    parser.add_argument('--report',type=Path);parser.add_argument('--port',type=int);parser.add_argument('--seconds',type=int)
    parser.add_argument('--config',type=Path,help='Console TOML with optional developer_voice and pngtuber sections')
    args=parser.parse_args()
    cfg={}
    if args.config:
        if args.action!='serve':parser.error('--config is for the console')
        with args.config.open('rb') as file:cfg=tomllib.load(file)
    route=cfg.get('developer_voice',{})
    port=args.port if args.port is not None else route.get('port',8767)
    seconds=args.seconds if args.seconds is not None else route.get('session_seconds',7200)
    if type(port) is not int or not 1024<=port<=65534 or type(seconds) is not int or not 10<=seconds<=7200:
        parser.error('port must be 1024..65534 and seconds 10..7200')
    if route.get('input_source','windows') not in ('windows','dell') or route.get('end_mode','manual') not in ('manual','auto','handsfree'):
        parser.error('Invalid microphone or end mode')
    silence=route.get('end_silence_ms',800)
    if type(silence) is not int or not 300<=silence<=2500:parser.error('end_silence_ms must be 300..2500')
    if route.get('input_source')=='dell' or 'dell_device' in route:
        from ravora.dell_microphone import parse_device
        try:parse_device(route.get('dell_device'))
        except ValueError:parser.error('Set dell_device to an ALSA card ID and PCM number')
    binding=None
    if 'pngtuber' in cfg:
        from ravora.pngtuber import pngtuber_binding
        binding=pngtuber_binding(args.config.resolve().parent,cfg)
    if args.action=='serve':
        if args.report:parser.error('--report is for replay commands only')
        try:asyncio.run(console(port,seconds,route=route,avatar_binding=binding))
        except ModuleNotFoundError as exc:
            if exc.name=='websockets':parser.exit(2,'Install the optional console dependency: python3 -m pip install ".[console]"\n')
            raise
        except KeyboardInterrupt:pass
    else:
        if args.action=='relationships':
            result=relationship_replay()
        elif args.action=='media-timing':
            result=media_timing_replay()
        else:
            result=asyncio.run(early_reaction_replay() if args.action=='early-reaction' else replay())
        text=json.dumps(result,indent=2)+'\n'
        if args.report:
            with args.report.open('x') as target:target.write(text)
        print(text,end='')


if __name__=='__main__':main()
