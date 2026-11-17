"""Synthetic transports and tone-free audio fixtures shared by tests."""
import io,json,wave
from pathlib import Path

def wav_bytes():
    out=io.BytesIO()
    with wave.open(out,'wb') as w:w.setnchannels(1);w.setsampwidth(2);w.setframerate(48000);w.writeframes(b'\0\0'*480)
    return out.getvalue()


class HTTP:
    def __init__(self,reply=None,stream=None):self.reply=reply;self.data=stream;self.calls=[]
    def json(self,url,headers,payload=None):self.calls.append((url,headers,payload));return self.reply
    def open(self,url,headers,payload=None):self.calls.append((url,headers,payload));return io.BytesIO(self.data)


class Text:
    def stream(self,*_):yield 'Hello there. ';yield 'A second sentence.'


class Reaction:
    def classify(self,_):return {'reaction':'joy','source':'test'}


class Speech:
    def __init__(self,cancel=None):self.calls=[];self.cancel=cancel
    def create(self,text,output):
        self.calls.append(text);Path(output).write_bytes(wav_bytes())
        if self.cancel:self.cancel.set()
        return {'duration':.01,'source':'test','visemes':[]}


# Fictional comment API fixtures used by relationship receipt/ban tests.
import datetime as dt
NOW=1780000000.
CHAT='test-live-chat'

def message(mid='m1',author='channel-one',text='Hello',at=NOW):
    return {'id':mid,'snippet':{'liveChatId':CHAT,'type':'textMessageEvent','authorChannelId':author,'publishedAt':dt.datetime.fromtimestamp(at,dt.timezone.utc).isoformat(),'textMessageDetails':{'messageText':text}},'authorDetails':{'channelId':author,'displayName':'Same name'}}

def page(items,token='next',interval=5000,**kwargs):return {'items':items,'nextPageToken':token,'pollingIntervalMillis':interval,**kwargs}
