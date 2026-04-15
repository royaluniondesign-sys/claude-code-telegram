import time
from random import uniform

def calculate_backoff_time(retries):
    base_delay = 1  # Base delay in seconds
    max_delay = 30  # Maximum delay in seconds
    return min(base_delay * (2 ** retries), max_delay)
