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


def retrieval_query(text):
    words=re.findall(r'[^\W_]+',text.lower(),re.UNICODE)[:100]
    terms=list(dict.fromkeys(ALIASES.get(word,word) for word in words if word not in STOP_WORDS))
    wants_recall=bool(re.search(r'\b(?:remember|recall|earlier|previously|favou?rite)\b|\blast time\b',text,re.I))
    return ' '.join(terms[:16]),wants_recall
