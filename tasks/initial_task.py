from tasks.celery_app import app

@app.task(name="tasks.initialize_chain")
def initialize_chain(chain_arg):
    '''
    Entry point that passes the arguments as is
    '''
    return chain_arg


@app.task(name="tasks.initialize_frame_chain")
def initialize_frame_chain(frame_arg):
    '''
    Entry point that passes the arguments as is
    '''
    return frame_arg
