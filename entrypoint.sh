#!/bin/bash

# start the api
python ice_ai_api.py &
API_PID=$!

# scrape scheduler disabled — uncomment to re-enable autotrader_classics.py and
# car_and_classic.py daily between 06:00-07:00 UK time via scheduler.py
# python scheduler.py &
# SCHED_PID=$!
#
# # fail fast: exit as soon as either child dies so the orchestrator (ECS/Copilot)
# # restarts the container with a clean state instead of running half-up
# wait -n $API_PID $SCHED_PID
# EXIT_CODE=$?
#
# # tear down whichever child is still alive before we exit
# kill $API_PID $SCHED_PID 2>/dev/null
# wait 2>/dev/null
#
# echo "container exiting; first child to die returned $EXIT_CODE"
# exit $EXIT_CODE

wait $API_PID
EXIT_CODE=$?

echo "container exiting; ice_ai_api returned $EXIT_CODE"
exit $EXIT_CODE
