"""Small English preference extractor. It has no network or free-form stored payload."""
from dataclasses import dataclass
import re


POLICY='english-preferences-v1'
# Canonical, benign topics: arbitrary captures (names, addresses, health, work, etc.)
# never become memory. Extending this list is a policy change, not a model guess.
TOPICS={
    'coffee':('drink',r'coffee'), 'tea':('drink',r'tea'),
    'green tea':('drink',r'green tea'), 'hot chocolate':('drink',r'hot chocolate'),
    'jazz':('music',r'jazz'), 'classical music':('music',r'classical music'),
    'piano':('music',r'(?:playing (?:the )?)?piano'),
    'guitar':('music',r'(?:playing (?:the )?)?guitar'),
    'reading':('hobby',r'reading(?: books)?'), 'cooking':('hobby',r'cooking'),
    'baking':('hobby',r'baking'), 'gardening':('hobby',r'gardening'),
    'hiking':('hobby',r'hiking'), 'walking':('hobby',r'walking'),
    'photography':('hobby',r'photography'), 'drawing':('hobby',r'drawing'),
    'painting':('hobby',r'painting'), 'chess':('hobby',r'(?:playing )?chess'),
}
STOP_WORDS=set('a an the i me my you your we they it is are was do does did what which who how can could would should please remember recall earlier previously last time said told about and or to for of in on like likes liked love enjoy prefer favorite favourite'.split())
ALIASES={'drinks':'drink','beverages':'drink','beverage':'drink','music':'music','musical':'music',
         'hobbies':'hobby','books':'reading','book':'reading'}


@dataclass(frozen=True)
class Preference:
    topic: str
    polarity: str

    def content(self):
        if self.topic not in TOPICS or self.polarity not in ('like','dislike'):
            raise ValueError('INVALID_EXTRACTED_PREFERENCE')
        return {'summary':f'Viewer said they {self.polarity} {self.topic}.',
                'tags':[self.topic,TOPICS[self.topic][0],'preference'],'confidence':.9}


@dataclass(frozen=True)
class Extraction:
    preferences: tuple[Preference,...]=()
    reason: str='NO_SUPPORTED_PREFERENCE'


class LocalPreferenceExtractor:
    policy=POLICY

    def extract(self,text):
        if not isinstance(text,str) or not 0<len(text)<=1000:raise ValueError('INVALID_EXTRACTION_INPUT')
        text=text.replace('’',"'").lower().strip()
        if re.search(r"\b(?:do not|don't|never) (?:remember|save|store|keep)\b|\b(?:forget|off the record|private|secret|password|api[ _-]?key|bearer|address|phone|medical|diagnos|medication)\b|@|https?://|\d{4,}",text):
            return Extraction(reason='SENSITIVE_OR_OPT_OUT')
        # Only complete, first-person preference statements. Questions, quoted text,
        # hypothetical clauses and injected instructions have no matching production.
        if any(x in text for x in ('?', '"', '“', '”', '`', ':','\n','\r')):
            return Extraction(reason='AMBIGUOUS_OR_QUOTED')
        statements=re.split(r'[.!;]+\s*|\s+but\s+',text)
        choices={}
        patterns=[(re.compile(r"i (?:really )?(?:like|love|enjoy|prefer) (.+)"),'like'),
                  (re.compile(r"i (?:don't|do not) (?:like|enjoy) (.+)"),'dislike'),
                  (re.compile(r'i dislike (.+)'),'dislike'),
                  (re.compile(r'my favou?rite (?:drink|beverage|music|hobby) is (.+)'),'like')]
        for statement in statements:
            for pattern,polarity in patterns:
                match=pattern.fullmatch(statement.strip())
                if not match:continue
                parts=re.split(r'\s+and\s+|,\s*(?:and\s+)?',match[1])
                found=[]
                for part in parts:
                    matches=[topic for topic,(_,expression) in TOPICS.items() if re.fullmatch(expression,part.strip())]
                    if len(matches)!=1:break
                    found.append(matches[0])
                else:
                    if 1<=len(found)<=4:
                        for topic in found:choices[topic]=polarity
        if not choices:return Extraction()
        return Extraction(tuple(Preference(topic,polarity) for topic,polarity in list(choices.items())[:4]),'SUPPORTED_PREFERENCE')


# Common conversational words provide no evidence for retrieving a different topic.
DIALOGUE_WORDS = set((
    "am be been being but that this these those there then with at as if he she his her him them their us our "
    "by from so s t re ve ll m d don didn just really actually oh ah uh um well okay ok yes yeah yep yup sure "
    "no know want wanted say says saying talk talked talking tell telling again before now already maybe still "
    "mean means meant thing something anything go going sounds good make making get got will all also had has have were"
).split())
ACK_WORDS = set((
    "i we will do it that is am m ll d oh ah uh um yeah yes yep yup right sure ok okay maybe "
    "thank thanks you alright really see guess so hahaha ha"
).split())


def laughter_only(text):
    return bool(re.fullmatch(r'(?:(?:ha)+|lol)(?:[ ,.!]+(?:(?:ha)+|lol))*[.!]*',text.strip(),re.I))


def acknowledgment(text):
    words = re.findall(r"[^\W_]+", text.casefold())
    return laughter_only(text) or (bool(words) and "?" not in text and set(words) <= ACK_WORDS)


def banter_preference(text):
    """Only an explicit, standalone request or first-person preference can persist."""
    words = text.casefold().replace("’", "'").strip().rstrip("!.。！").strip()
    words = re.sub(r',? please$', '', words)
    if re.fullmatch(r"(?:please )?(?:stop teasing me|(?:don't|do not) tease me)(?: anymore)?"
                    r"|i (?:dislike|(?:don't|do not) (?:like|enjoy)) (?:being teased|(?:it )?when you tease me|this kind of teasing|our banter)"
                    r"|(?:もう)?からかわないで(?:ほしい|ください)?", words):
        return 'dislike'
    if re.fullmatch(r"i (?:really )?(?:like|love|enjoy) (?:being (?:gently )?teased|(?:it )?when you tease me|this kind of teasing|our banter)"
                    r"|(?:こういう|最後の方の|軽い)からかい(?:が|は)(?:好き|面白い)", words):
        return 'like'
    return None


def reply_cue(text):
    """High precision hints about the current turn, never durable preferences."""
    words = " ".join(re.findall(r"[^\W_]+", text.casefold().replace("’", "'")))
    if re.search(r"\b(?:what (?:did|do) you mean(?: by that| exactly)?|what (?:did|does) that mean|(?:please |can you |could you )?explain "
                 r"(?:it|that|what you (?:said|meant)))(?: please)?$|\bwhat did you mean by\b", words) or re.search(
                     r"どういう意味|今の(?:冗談|言葉).*(?:説明|意味)", text):
        return 'explain'
    if re.match(r"(?:no )+(?:i (?:meant|mean)|that s not|that is not)|i meant to (?:say|ask)", words) or re.match(r"(?:いや|違う)[、， ]", text):
        return 'correct'
    if re.fullmatch(r"(?:(?:haha(?:ha)*|ha ha|lol|okay|oh|well|yeah|fair enough) )*"
                    r"(?:you (?:really )?(?:got|caught|chase) me(?: there)?|look who s talking|"
                    r"two can play (?:at )?that game|touché|touche)(?: haha(?:ha)*)?", words):
        return 'play_along'
    return None


def historical_reference(text):
    return bool(re.search(r"\b(?:remember|recall|remind me|earlier|previously|ago|used to|last (?:time|session|week|month)|"
                          r"yesterday|previous (?:chat|session|conversation)|(?:other|another) (?:day|session))\b|"
                          r"\b(?:told|mentioned|discussed|asked|talked about)\b.{0,35}\bbefore\b", text, re.I))


def session_recap(text):
    if re.search(r"\b(?:last (?:time|session|week|month)|yesterday|previous (?:chat|session))\b", text, re.I):
        return False
    if re.search(r"\b(?:do you|can you) (?:remember|recall) (?:it|that|this)\s*[?!.]*$", text,re.I):
        return True
    # Match a retrospective question even when STT inserts punctuation between subject and verb.
    return bool(re.search(r"\bwhat (?:did (?:i|we)|was i|were we)\b.{0,22}?\b(?:say|said|talk(?:ing|ed)?|"
                          r"discuss(?:ing|ed)?|mention(?:ed)?|speaking)\b|\bwhat (?:was our|was the) (?:last )?topic\b|"
                          r"\b(?:recap|summari[sz]e) (?:this|our|the current) (?:chat|conversation)\b", text, re.I))


def dialogue_terms(text):
    return list(dict.fromkeys(w for w in re.findall(r'[^\W_]+', text.casefold())
                              if w not in STOP_WORDS and w not in DIALOGUE_WORDS))


def retrieval_query(text):
    words=re.findall(r'[^\W_]+',text.lower(),re.UNICODE)[:100]
    terms=list(dict.fromkeys(ALIASES.get(word,word) for word in words if word not in STOP_WORDS))
    wants_recall=bool(re.search(r'\b(?:remember|recall|earlier|previously|favou?rite)\b|\blast time\b',text,re.I))
    return ' '.join(terms[:16]),wants_recall
