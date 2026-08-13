#!/bin/bash

# start a virtual x server so headed chromium has a display on ecs/fargate
Xvfb :99 -screen 0 1280x800x24 -ac -nolisten tcp &
XVFB_PID=$!
for _ in $(seq 1 50); do
  if xdpyinfo -display :99 >/dev/null 2>&1; then
    break
  fi
  sleep 0.1
done
if ! xdpyinfo -display :99 >/dev/null 2>&1; then
  echo "warning: Xvfb on :99 did not become ready; headed chrome will fail"
fi

if command -v dbus-launch >/dev/null 2>&1; then
  eval "$(dbus-launch --sh-syntax)"
fi

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

kill "$XVFB_PID" 2>/dev/null
wait "$XVFB_PID" 2>/dev/null

echo "container exiting; ice_ai_api returned $EXIT_CODE"
exit $EXIT_CODE
