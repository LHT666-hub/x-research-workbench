"""Narrow authorization check for a verified user-pause checkpoint."""

PAUSE_CLOSE_ERROR = 'Exception: BrowserContext.close: Connection closed while reading from the driver'


def validate_pause_resume(raw, receipt, current_sha, paused_sha, plan_sha):
    if not all(receipt.get(key) for key in ('ctrl_c_sent','stopped_verified','all_pre_pause_posts_preserved')):
        raise ValueError('Missing verified safe-pause evidence')
    if current_sha != paused_sha or raw.get('plan_sha256') != plan_sha:
        raise ValueError('Checkpoint or plan changed since the verified pause')
    if raw.get('finalized') or len(raw.get('posts',[])) != receipt.get('saved_posts'):
        raise ValueError('Finalized or inconsistent saved checkpoint')
    hold=raw.get('guard_hold')
    if hold != receipt.get('guard_hold_preserved'):
        raise ValueError('New protection must not be cleared')
    if hold and (hold.get('error') != PAUSE_CLOSE_ERROR or hold.get('reason') != 'independent_continuation_requires_review'):
        raise ValueError('Business protection requires separate review')
    keys=[str(post.get('post_id') or post.get('post_url') or '') for post in raw.get('posts',[])]
    if not all(keys) or len(keys)!=len(set(keys)):
        raise ValueError('Saved posts have missing or duplicate identities')
