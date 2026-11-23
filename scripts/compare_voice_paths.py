#!/usr/bin/env python3
"""Compare the developer and studio paths using a synthetic turn without outbound requests."""
import asyncio
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import threading
import time
import uuid

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))
from ravora.conversation import ConversationBridge, MemoryStore
from ravora.edge_grpc import pcm_wave
from ravora.fast_voice import FastVoiceSession
from ravora.fast_voice_server import MockVoiceClients
from ravora.input_pipeline import InputPipeline
from ravora.live_media import FPS, decode_wav
from ravora.live_program import LiveProgram
from ravora.live_voice import LiveChirpSpeech
from ravora.ray_character import public_fixture
from ravora.safety import TurnFence
from ravora.settings import ProviderSettings

TRANSCRIPT = 'I like coffee and jazz.'
REPLY = 'I like that rhythm too.'
BLOCK = np.rint(np.sin(2*np.pi*220*np.arange(2400)/24000)*2500).astype('<i2').tobytes()
SETTINGS = ProviderSettings(memory_enabled=False, early_reaction_enabled=False, character_initiative_enabled=False)


class Providers(MockVoiceClients):
    async def generate(self, text, **kwargs):
        assert text == TRANSCRIPT
        yield public_fixture(REPLY)

    generate.ray_public_output = True

    async def synthesize_stream(self, texts):
        assert ''.join([text async for text in texts]) == REPLY
        for _ in range(10):
            await asyncio.sleep(.05)
            yield BLOCK

    def stream(self, text, history):
        assert text == TRANSCRIPT
        yield public_fixture(REPLY)

    stream.ray_public_output = True

    def classify(self, *args):
        return {'reaction':'neutral','source':'fixture'}


class FixtureSpeech(LiveChirpSpeech):
    async def _pcm(self, text, on_pcm):
        async def sentence():
            yield text
        result = bytearray()
        async for packet in Providers().synthesize_stream(sentence()):
            result.extend(packet)
            if on_pcm:
                on_pcm(packet)
        return result


def timing(metrics):
    return {key:metrics.get(key) for key in ('first_text_ms','public_output_ready_ms','first_pcm_ms','first_audio_ready_ms')}


async def developer(root, pcm):
    audio, events = bytearray(), []
    async def event(value):
        events.append(value)
    async def sound(token, packet):
        audio.extend(packet)
    session = FastVoiceSession(SETTINGS,MemoryStore(root/'dev.db'),root,Providers(),event,sound,mock=True)
    try:
        key = uuid.uuid4().hex
        await session.start_audio(key,'en-US')
        for offset in range(0,len(pcm),3200):
            await session.audio_chunk(key,pcm[offset:offset+3200])
            await asyncio.sleep(0)
        await session.end_audio(key)
        await asyncio.wait_for(session.task,5)
        assert session.hash.digest() == hashlib.sha256(pcm).digest()
        assert bytes(audio) == BLOCK*10 and session.played(session.token,session.expected_frames)
        return timing(next(e['metrics'] for e in events if e['kind']=='complete'))
    finally:
        await session.close()


def studio(root, wav, progressive):
    providers = Providers()
    speech = FixtureSpeech(SETTINGS)
    speech.streams_program_pcm = progressive
    store = MemoryStore(root/f'studio-{progressive}.db')
    bridge = ConversationBridge(SETTINGS,store,root,TurnFence(),threading.RLock(),providers=(providers,providers,speech))
    # Compare the unprocessed provider waveform to the developer transport.
    program = LiveProgram(InputPipeline(SETTINGS,store,bridge,[]),root,ray_reverb_mix=0)
    def transcribe(packet, **kwargs):
        assert packet.data == wav
        return {'speech':True,'text':TRANSCRIPT}
    program.stt.transcribe = transcribe
    try:
        program.submit_audio(uuid.uuid4().hex,wav,already_aired=True)
        output = bytearray()
        for _ in range(5*FPS):
            start = time.monotonic()
            stage, frame = program.next_frame()
            if stage.speaker == 'Ray':
                output.extend(frame)
            program.encoded(stage)  # Local PCM sink at 30 Hz; no video encoder or network.
            if program.completed or program.failed:
                break
            time.sleep(max(0, 1/FPS - (time.monotonic()-start)))
        assert program.completed == 1 and output == decode_wav(pcm_wave(BLOCK*10,24000))
        with store.connect() as db:
            raw, = db.execute('SELECT metrics FROM conversation_turns WHERE played=1').fetchone()
        metrics = json.loads(raw)
        return {**timing(metrics), **metrics['live_program']}
    finally:
        program.close()


if __name__ == '__main__':
    samples = np.arange(11200)
    tone = np.rint(6000 * np.sin(2 * np.pi * 220 * samples / 16000)).astype('<i2')
    pcm = bytes(16000) + tone.tobytes() + bytes(16000)
    wav = pcm_wave(pcm, 16000)
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        result = {'input_pcm_sha256':hashlib.sha256(pcm).hexdigest(), 'fixture':True,
                  'developer':asyncio.run(developer(root,pcm)),
                  'studio_buffered':studio(root,wav,False), 'studio_progressive':studio(root,wav,True)}
        print(json.dumps(result,indent=2))
