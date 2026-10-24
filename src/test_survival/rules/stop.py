"""Round decisions are determined solely by the configured limits and funnels."""

def decision(config,round_no,retry,compiled_history,kept,dev):
    if compiled_history[-1]==0 and retry<config['retry_on_zero_compile']:
        return 'retry'
    stop=config['stop_after_zero_compile']
    if stop>0 and len(compiled_history)>=stop and not any(compiled_history[-stop:]):
        return 'zero-compile'
    if config['until_dev_matched'] and kept>=dev:
        return 'dev-matched'
    if round_no>=config['max']:
        return 'max-rounds'
    return 'next'
