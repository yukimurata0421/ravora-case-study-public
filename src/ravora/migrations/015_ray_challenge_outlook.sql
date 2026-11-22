UPDATE character_profiles
SET traits = json_insert(traits, '$[#]', 'embraces_challenges', '$[#]', 'optimism'),
    revision = revision + 1,
    updated_at = unixepoch()
WHERE character_id = 'ray';
