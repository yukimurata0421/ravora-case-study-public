"""1920×1080 Ray broadcast artwork, shared by the encoder and browser preview."""
from dataclasses import dataclass
from functools import lru_cache
from itertools import islice
import hashlib
import re

import numpy as np
from PIL import Image, ImageDraw, ImageFont

SIZE = (1920, 1080)
AVATAR_X = 790
CREAM, MUTED, AMBER, BLUE = '#F5EDE3', '#BFB2A5', '#F4B76A', '#93C1EE'
DIM = '#9A8C7F'
PALETTE = (('#6E5A8A','#C9B8E8'), ('#3F6E66','#9FD6C9'), ('#8A5A3F','#F2C3A6'),
           ('#4A5F8C','#A9C2F0'), ('#7A6A3A','#E6D49A'), ('#5C7A3F','#C4E29A'),
           ('#835763','#EDB8C9'), ('#446D80','#AEDAEA'))
FACES = ('neutral', 'joy', 'surprise', 'thinking', 'concerned')
CJK = re.compile(r'[^\x00-\u024f\u2000-\u206f]')
CAPTION_WIDTH = 1170
CAPTION_PROFILES = ((38, 2), (34, 4), (32, 5))
LATIN_WORD_END = re.compile(r"[A-Za-z0-9_'’-]+$")
FULL_WIDTH = re.compile(r'[\u3000-\u30ff\u3400-\u9fff\uff01-\uff60]+')
NO_LINE_START = '。、！？：；，．」』】）》〉,.!?;:%)]'
NO_LINE_END = '「『【（《〈(['


@dataclass(frozen=True)
class ChatLine:
    name: str
    text: str


@lru_cache(maxsize=48)
def font(size, bold=False, cjk=False):
    if cjk:
        style = 'Bold' if bold else 'Regular'
        return ImageFont.truetype(f'/usr/share/fonts/opentype/noto/NotoSansCJK-{style}.ttc',size)
    return ImageFont.truetype('DejaVuSans-Bold.ttf' if bold else 'DejaVuSans.ttf',size)


def typeface(text, size, bold=False):
    return font(size,bold,bool(CJK.search(text)))


def _caption_rows(text, face, width, *, prefer_phrases=False):
    """Break at measured widths, keeping words and Japanese punctuation together."""
    start = 0
    while start < len(text):
        # Bound the width probes to roughly one row, even for a very long cue.
        cells = max(1, width // face.size)
        high = min(len(text), start + cells)
        low = start
        if FULL_WIDTH.fullmatch(text, start, high):
            # A full-width row only needs one measurement in the usual case.
            if face.getlength(text[start:high]) <= width:
                low = high
        else:
            while face.getlength(text[start:high]) <= width:
                low = high
                if high == len(text):
                    break
                high = min(len(text), high + cells)
        while low + 1 < high:
            mid = (low + high) // 2
            if face.getlength(text[start:mid]) <= width:
                low = mid
            else:
                high = mid
        end = max(start + 1, low)
        if end < len(text):
            while end > start + 1 and (text[end] in NO_LINE_START or text[end-1] in NO_LINE_END):
                end -= 1
            if LATIN_WORD_END.fullmatch(text[end]):
                word = LATIN_WORD_END.search(text[start:end])
                if word and word.start():
                    end = start + word.start()
            if prefer_phrases:
                tail = start + (end - start) // 2
                stop = max(text.rfind(mark, tail, end) for mark in '。、！？')
                if stop >= tail and text[stop+1] not in NO_LINE_START:
                    end = stop + 1
        yield text[start:end].rstrip()
        start = end
        while start < len(text) and text[start].isspace():
            start += 1


def caption_layout(text, *, host_transcript=False):
    text = ' '.join(text.split())
    cjk = host_transcript or bool(CJK.search(text))
    profiles = ((22, 3),) if host_transcript else CAPTION_PROFILES
    for size, count in profiles:
        face = font(size, not host_transcript, cjk)
        rows = _caption_rows(text, face, CAPTION_WIDTH)
        first = list(islice(rows, count + 1))
        if len(first) <= count:
            if len(first) > 1 and any(mark in text for mark in '。、！？'):
                natural = list(islice(_caption_rows(text, face, CAPTION_WIDTH, prefer_phrases=True), len(first)+1))
                if len(natural) == len(first):
                    first = natural
            if len(first) > 1 and len(first[-1]) < len(first[-2]) / 3:
                average_width = sum(face.getlength(row) for row in first) / len(first)
                balanced = list(_caption_rows(text, face, min(CAPTION_WIDTH, round(average_width * 1.06))))
                if len(balanced) == len(first):
                    first = balanced
            return (size, cjk), ('\n'.join(first),)
    all_rows = [*first, *rows]
    return (size, cjk), tuple('\n'.join(all_rows[i:i+count]) for i in range(0, len(all_rows), count))


def lines(text, face, width, limit=None):
    """Measured wrapping, with safe breaks even for an overlong unspaced token."""
    result, current = [], ''
    for token in re.findall(r'\s+|[A-Za-z0-9_\x27’.,!?-]+|.',text):
        if face.getlength(current + token) <= width:
            current += token
            continue
        if current.strip():
            result.append(current.strip())
            if limit and len(result) == limit:
                return result[:-1] + [ellipsize(result[-1]+'…',face,width)]
        current = ''
        for char in token.lstrip():
            if current and face.getlength(current + char) > width:
                result.append(current)
                if limit and len(result) == limit:
                    return result[:-1] + [ellipsize(result[-1]+'…',face,width)]
                current = ''
            current += char
    return result + ([current.strip()] if current.strip() else [])


def ellipsize(text, face, width):
    if face.getlength(text) <= width:
        return text
    low, high = 0, len(text)
    while low < high:
        mid = (low+high+1)//2
        if face.getlength(text[:mid]+'…') <= width:
            low = mid
        else:
            high = mid-1
    return text[:low].rstrip()+'…'


def ink(draw, xy, text, size, color=CREAM, bold=False):
    draw.text(xy,text,font=typeface(text,size,bold),fill=color,anchor='lt')


def badge(draw, x, y, title, color=AMBER, dark='#1A120C', size=16):
    width = round(typeface(title,size,True).getlength(title)) + 28
    draw.rounded_rectangle((x,y,x+width,y+size+14),radius=9,fill=color)
    ink(draw,(x+14,y+7),title,size,dark,True)
    return width


def author_colors(name):
    return PALETTE[hashlib.blake2s(name.encode(),digest_size=1).digest()[0] % len(PALETTE)]


def backdrop():
    # The reference's radial gradient and three soft light spots are static.
    y,x = np.ogrid[:1080,:1920]
    distance = np.sqrt(((x-1320)/900)**2+((y-460)/700)**2)
    c = np.stack([np.interp(distance,[0,.45,1],[a,b,d]) for a,b,d in
                  zip((58,42,32),(36,26,21),(21,17,15))],axis=-1)
    for cx,cy,r,color,alpha in ((1770,200,76,(244,183,106),.1),(1025,115,44,(244,183,106),.08),
                                (1810,550,32,(147,193,238),.08)):
        weight = (alpha*np.exp(-((x-cx)**2+(y-cy)**2)/(2*r*r)))[:,:,None]
        c += (color-c)*weight
    return Image.fromarray(c.astype('uint8'))


class Overlay:
    def __init__(self, directory=None, *, live=False):
        self.live = live
        self.backgrounds = {}
        if directory:
            for face in FACES:
                path = directory / (face+'.png')
                if path.exists():
                    with Image.open(path) as image:
                        if image.size != SIZE or image.format != 'PNG':
                            raise ValueError('SCENE_REQUIRES_1920X1080_PNG')
                        self.backgrounds[face] = image.convert('RGB')
            if 'neutral' not in self.backgrounds:
                raise ValueError('SCENE_NEUTRAL_PNG_REQUIRED')
        else:
            self.backgrounds['neutral'] = backdrop()
        self._chat_key = self._panel_key = None

    def _header(self, draw):
        ink(draw,(64,58),'R A Y',44,bold=True)
        label = 'LIVE' if self.live else 'PREVIEW'
        end = 241 if self.live else 282
        draw.rounded_rectangle((180,63,end+14,95),radius=16,fill='#C8443A' if self.live else '#994942')
        draw.ellipse((192,75,200,83),fill='white')
        ink(draw,(207,72),label,15,'white',True)
        ink(draw,(end+34,69),'Late-night chat · ask me anything',20,MUTED)
        ink(draw,(64,150),'C H A T',14,DIM,True)

    def _chat(self, stage):
        layer = Image.new('RGBA',(562,601))
        y = 0
        if stage.prompt_title:
            title = stage.prompt_title
            ja = stage.language.startswith('ja')
            banner = Image.new('RGBA', (561, 64))
            bd = ImageDraw.Draw(banner)
            bd.rounded_rectangle((0, 0, 559, 63), radius=14,
                                 fill=(30, 24, 20, 224), outline=(244, 183, 106, 110), width=1)
            bw = badge(bd, 12, 16, 'お題' if ja else 'TOPIC', AMBER, '#1A120C', 12)
            tfont = typeface(title, 16, True)
            bd.text((22 + bw, 20), ellipsize(title, tfont, 520 - bw), font=tfont, fill=CREAM, anchor='lt')
            layer.alpha_composite(banner, (0, y))
            y += 74
        count = len(stage.chat)
        # Reserve room for the reply target, then the newest messages. Paint in chat order.
        gap = 12
        remaining = layer.height - y
        cards = {}
        priority = sorted(range(count), key=lambda i: (i == stage.selected, i), reverse=True)
        for i in priority:
            text = stage.chat[i].text
            face = typeface(text, 19)
            max_lines = 2 if count < 5 or i in (stage.selected, count - 1) else 1
            body = lines(text, face, 468, limit=max_lines)
            height = 54 + 25 * len(body)
            if height <= remaining:
                cards[i] = face, body, height
                remaining -= height + gap

        for i in sorted(cards):
            comment = stage.chat[i]
            f, body, height = cards[i]
            picked = i==stage.selected
            avatar, name_color = author_colors(comment.name)
            card = Image.new('RGBA',(561,height))
            draw = ImageDraw.Draw(card)
            draw.rounded_rectangle((0,0,559,height-1),radius=16,
                fill=(44,51,57,228) if picked else (32,25,21,209),
                outline=BLUE if picked else (255,240,225,24),width=2 if picked else 1)
            draw.ellipse((18,14,54,50),fill=avatar)
            first = comment.name[:1].upper()
            draw.text((36,32),first,font=typeface(first,15,True),anchor='mm',fill=CREAM)
            name = ellipsize(comment.name,typeface(comment.name,15,True),300)
            ink(draw,(68,15),name,15,name_color,True)
            if picked:
                badge(draw,round(80+typeface(name,15,True).getlength(name)),8,
                      'REPLYING' if stage.replying else 'PICKED',BLUE,'#10202F',12)
            for n, line in enumerate(body):
                draw.text((68,38+n*25),line,font=f,fill=CREAM,anchor='lt')
            opacity = 1 if picked else max(.45, 1-(count-i-1)*.135)
            if opacity<1:
                card.putalpha(card.getchannel('A').point(lambda v:round(v*opacity)))
            layer.alpha_composite(card,(0,y))
            y += height + gap
        return layer

    def _subtitles(self, stage):
        picked = stage.chat[stage.selected] if 0<=stage.selected<len(stage.chat) else None
        if not (stage.caption or stage.thinking or stage.previous or stage.host_caption):
            return None
        lingering = bool(stage.previous and not stage.caption and not stage.thinking)
        text = stage.previous if lingering else stage.caption
        size, cjk = stage.caption_font
        face = font(size, True, cjk or bool(CJK.search(text)))
        if text:
            body = text.split('\n')
        else:
            quote = '“'+picked.text+'”' if picked and stage.thinking else ''
            face = typeface(quote, size, True)
            body = lines(quote,face,1275,limit=2)
        leading = size + 15
        host_rows = stage.host_caption.splitlines()
        recap = bool(stage.previous and not lingering and not host_rows)
        host_height = 32*len(host_rows) + 10 if host_rows else 0
        height = (leading*len(body) + 54 + host_height + (42 if recap else 0)
                  + (40 if picked else 0) + (48 if stage.thinking else 0))
        panel = Image.new('RGBA',(1360,height))
        d = ImageDraw.Draw(panel)
        d.rounded_rectangle((0,0,1359,height-1),radius=24,fill=(20,15,13,224),outline=(255,240,225,32))
        y = 26
        if host_rows:
            width = badge(d, 36, y - 3, 'HOST', BLUE, size=14)
            face_host = font(22, False, True)
            for line in host_rows:
                d.text((52 + width, y), line, font=face_host, fill=CREAM, anchor='lt')
                y += 32
            y += 10
        elif recap:
            previous = ' '.join(stage.previous.split())
            f = typeface(previous,22)
            ink(d,(36,y),ellipsize(previous,f,1270),22,DIM)
            y += 42
        if stage.thinking and picked:
            width = badge(d,36,y-4,'FROM CHAT',BLUE,'#10202F',13)
            ink(d,(52+width,y+1),ellipsize(picked.name,typeface(picked.name,16,True),700),16,author_colors(picked.name)[1],True)
            y+=40
        if text:
            speaker = 'Ray' if lingering else stage.speaker
            badge(d,36,y+1,speaker.upper(),AMBER if speaker=='Ray' else BLUE)
        offset=0
        for line in body:
            x=138 if text else 36
            begin,end = stage.highlight
            a,b = max(0,min(len(line),begin-offset)), max(0,min(len(line),end-offset))
            baseline = y + size + 2
            if begin<0 or lingering:
                d.text((x,baseline),line,font=face,fill=MUTED if lingering else CREAM,anchor='ls')
            elif end<=offset or begin>=offset+len(line):
                color = '#A6988B' if end<=offset else CREAM
                d.text((x,baseline),line,font=face,fill=color,anchor='ls')
            else:
                # A single baseline keeps kana and Latin aligned across color runs.
                d.text((x,baseline),line[:a],font=face,fill=CREAM,anchor='ls')
                x += face.getlength(line[:a])
                d.text((x,baseline),line[a:b],font=face,fill=AMBER,anchor='ls')
                right = x + face.getlength(line[a:b])
                d.line((x,baseline+7,right,baseline+7),fill=AMBER,width=3)
                d.text((right,baseline),line[b:],font=face,fill='#A6988B',anchor='ls')
            offset+=len(line)+1
            y+=leading
        if stage.thinking:
            badge(d,36,y,'RAY')
            for i, color in enumerate((AMBER,'#AA804F','#745A41')):
                d.ellipse((125+i*18,y+9,135+i*18,y+19),fill=color)
            ink(d,(189,y+5),'thinking',18,MUTED)
        elif picked and stage.caption:
            d.line(((155,y+16),(155,y+12),(141,y+12),(146,y+7)),fill=DIM,width=2)
            d.line(((141,y+12),(146,y+17)),fill=DIM,width=2)
            label='to '+picked.name+' · “'+picked.text+'”'
            f=typeface(label,16)
            ink(d,(166,y+5),ellipsize(label,f,1130),16,MUTED)
        return panel

    def compose(self, stage, portrait=None):
        chat_key=(stage.expression,stage.chat,stage.selected,stage.replying,stage.prompt_title,stage.language)
        if chat_key!=self._chat_key:
            self._base=self.backgrounds.get(stage.expression,self.backgrounds['neutral']).copy()
            self._header(ImageDraw.Draw(self._base))
            cards=self._chat(stage)
            self._base.paste(cards,(64,181),cards)
            self._chat_key=chat_key
        panel_key=(stage.chat[stage.selected] if 0<=stage.selected<len(stage.chat) else None,
                   stage.caption,stage.caption_font,stage.speaker,stage.highlight,stage.previous,stage.thinking,stage.host_caption)
        if panel_key!=self._panel_key:
            self._panel=self._subtitles(stage)
            self._panel_key=panel_key
        image=self._base.copy()
        if portrait is not None:
            image.paste(portrait, (AVATAR_X, 0))
        if self._panel is not None:
            image.paste(self._panel,(64,1024-self._panel.height),self._panel)
        return image
