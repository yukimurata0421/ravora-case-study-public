-- Keep usage attributable to its session after expiring the inbox text.
ALTER TABLE conversation_turns ADD COLUMN usage_session TEXT;
UPDATE conversation_turns AS t SET usage_session=i.session
FROM comment_inbox AS i WHERE i.receipt=t.source_event_id;
