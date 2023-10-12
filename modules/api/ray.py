from ray import serve
import ray

from modules.api.raypi import Raypi

import time



ray.init()
#ray.init("ray://localhost:10001")


def ray_only():
    serve.shutdown()
    serve.start()
    serve.run(Raypi.bind(), port=8000, route_prefix="/sdapi/v1")  #route_prefix="/sdapi/v1" # Call the launch_ray method to get the FastAPI app
    print("Done setting up replicas! Now accepting requests...")
    while True:
        time.sleep(1000)
