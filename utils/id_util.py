from sonyflake import Sonyflake
from datetime import datetime

sf = Sonyflake(start_time=datetime(2026, 1, 1))

def get_sonyflake(prefix: str = None) -> str:
    next_id = sf.next_id()
    return str(next_id) if prefix is None else prefix + str(next_id)

if __name__ == '__main__':
    print(get_sonyflake())