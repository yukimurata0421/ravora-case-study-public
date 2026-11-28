"""Stable character choices. Only the selected spoken direction leaves this module."""
from dataclasses import dataclass, fields
import re


@dataclass(frozen=True, slots=True)
class Personality:
    curiosity: float = .9
    independence: float = .7
    playfulness: float = .8
    empathy: float = .75
    competitiveness: float = .85
    sociability: float = .65
    openness_to_correction: float = .8
    accommodation: float = .5

    def __post_init__(self):
        for field in fields(self):
            value = getattr(self,field.name)
            if type(value) not in (float,int) or not 0 <= value <= 1:
                raise ValueError('INVALID_PERSONALITY_WEIGHT')

    def direction(self, text, intent, desire, *, initiative=False):
        if initiative:
            if self.playfulness >= self.sociability:
                return 'Try one playful variation on the premise.'
            return 'Give the room an easy way into the premise.'
        if intent == 'answer' and re.search(r'[?？]|^(?:what|who|how|why|when|where)\b|教えて|説明して', text, re.I):
            return ''
        if desire == 'correct_the_mistake':
            if self.openness_to_correction >= self.independence:
                return 'Consider the correction and acknowledge the specific mistake if it checks out.'
            return 'Check the disputed detail before explaining or revising your answer.'
        if re.search(r'(?i)\byou (?:lost|missed|failed)\b|(?:レイ|あなた|君)(?:が|は|の)(?:負け|失敗)',text):
            if self.competitiveness >= self.playfulness:
                return 'Own your setback and try one different move if the game is still on.'
        if re.search(r"(?i)\bI (?:lost|failed|missed)|\bmy (?:loss|setback)|(?:私|僕|俺)(?:が|は|の)(?:負け|失敗)|負けちゃった|負けたよ",text):
            return ('Acknowledge their setback and leave them room.' if self.empathy >= self.sociability
                    else 'Give them room to say how the setback felt.')
        if intent == 'disagree':
            return 'Give an honest view and stay open to evidence.'
        if intent == 'try_challenge' or re.search(r"(?i)\b(?:shall we|let.s try)|一緒に.{0,12}(?:やろう|試そう)", text):
            return ('Offer your own fair variation on the invitation.' if self.independence > self.accommodation
                    else 'Try the proposed rules if they suit the conversation.')
        if intent in ('share_interest','ask_question','state_opinion'):
            if self.curiosity >= max(self.playfulness,self.sociability):
                return 'Pick the detail you would most like to understand and say why.'
            return ('Explore a playful example.' if self.playfulness >= self.sociability
                    else 'Invite the speaker to add their own angle when appropriate.')
        return ''


RAY_PERSONALITY = Personality()
