import rclpy
from rclpy.node import Node
import numpy as np
import time
import sys

from vision_msgs.msg import Detection2DArray
from interfaces.msg import Map, Position, Status, LidarData
from std_msgs.msg import String

from robottle_utils import map_utils, controller_utils, vision_utils, lidar_utils
from robottle_utils.vizualiser import ImageVizualiser
from robottle_utils.rrt_star import RRTStar

### STATE MACHINES

INITIAL_ROTATION_MODE = "initial_rotation_mode"
TRAVEL_MODE = "travel_mode"
RANDOM_SEARCH_MODE = "random_search_mode"
BOTTLE_PICKING_MODE = "bottle_picking_mode"
BOTTLE_RELEASE_MODE = "bottle_release_mode"
BOTTLE_REACHING_MODE = "bottle_reaching_mode"
KICK_ASS_MODE = "rocks_will_die"
RECOVERY_SLAM = "recovery_mode"
RECOVERY_ROTATION = "recovery_rotation"

TIMER_STATE_OFF = "0"
TIMER_STATE_ON_TRAVEL_MODE = "1"
TIMER_STATE_ON_RANDOM_SEARCH_BOTTLE_ALIGNMENT = "2"
TIMER_STATE_ON_RANDOM_SEARCH_DELTA_ROTATION = "3"
TIMER_STATE_ON_BOTTLE_RELEASE = "4"
TIMER_STATE_ON_NO_ROTATION = "5"
TIMER_STATE_ON_KICK_ASS_MODE = "6"
TIMER_STATE_ON_TRAVEL_MODE_END = "7"

DETECTNET_ON = "ON"
DETECTNET_OFF = "OFF"

### HYPERPARAMETERS

# min area that a rotated rectangle must contain to be considered as valid
AREA_THRESHOLD = 110000
# distance at which, if the robot is closer than the goal, travel_mode ends
MIN_DIST_TO_GOAL = 25 # [pixels]
# distance to recycling
MIN_DIST_TO_RECYCLING = 12
# min distance between robot and point in the path to consider the robot as passed it
MIN_DIST_TO_POINT = 0.2 # [m]
# time constant of path computation update (the bigger, the less often the path is updated)
CONTROLLER_TIME_CONSTANT = 30
# path-tracker min angle diff for directing the robot
MIN_ANGLE_DIFF = 15 # [deg]
# Array containing indices of zones to visit: note that zones = [r, z2, z3, z4]
# z2 = grass, z3 = rocks
TARGETS_TO_VISIT = [3,1,0,2,4,0,5,6,0] # = grass, recycling, rocks, recycling
# delta degree for little random search rotations
DELTA_RANDOM_SEARCH = 40
# time to wait for detections on each flip of the camera
TIME_FOR_VISION_DETECTION = 1.1 # [s]
# maximum number of bottles robot can pick
MAX_BOTTLE_PICKED = 5
# maximum number of times controller enters random search mode inside 1 zone
N_RANDOM_SEARCH_MAX = 11

class Controller1(Node):
    """
    Controller of the ROBOT
    This node is a state machine with several states and transitions from one to each others.
    """
    def __init__(self):
        super().__init__("controller1")

        # Create subscription for the map
        self.subscription1 = self.create_subscription(Position,'robot_pos',
            self.listener_callback_position,1000)

        # Create subscription for the robot position
        self.subscription2 = self.create_subscription(Map,'world_map',
            self.listener_callback_map,1000)

        # Create subscription for the UART reader (get signals from MC)
        self.subscription3 = self.create_subscription(Status,'arduino_status',
            self.listener_arduino_status,1000)

        # Create subscription for detectnet
        self.subscription_camera = self.create_subscription(Detection2DArray, '/detectnet/detections',
            self.listener_callback_detectnet, 1000)

        # Create subscription for lidar
        self.subscription_lidar = self.create_subscription(LidarData, 'lidar_data',
            self.lidar_callback, 1000)

        # subscription for debugng
        self.loging_ling_sub = self.create_subscription(String, 'log_line', self.log_line, 5)

        # Create a publication for uart commands
        self.uart_publisher = self.create_publisher(String, 'uart_commands', 1000)

        # create publisher for controlling the camera
        self.cam_publisher = self.create_publisher(String, 'detectnet/camera_control', 1000)
        self.set_detectnet_state(DETECTNET_OFF)

        # create publisher for flipping camera
        self.camera_flip_topic = self.create_publisher(String, 'video_source/flip_topic', 1000)
        self.camera_flip_topic.publish(String(data="normal"))
        self.is_flipped = False 

        # publisher to control slam
        self.slam_control_publisher = self.create_publisher(String, 'slam_control', 1000)


        # keep track of where is the robot within the class
        self.x = 0
        self.y = 0
        self.theta = 0

        # variable for the controller
        self.initial_zones_found = False
        self.zones = np.array([])
        self.path = np.array([])
        self.targets = []
        self.goal = None
        self.robot_pos = None
        self.current_target_index = 0
        self.rotation_timer = None
        self.lidar_save_index = None
        self.n_random_search = 0
        self.bottles_picked = 0
        self.state = INITIAL_ROTATION_MODE
        self.rotation_timer_state = TIMER_STATE_OFF
        self.is_traveling_forward = False
        self.has_to_find_new_path = False
        self.lidar_should_detect_bottles = False
        self.rotation_index = 0

        # DEBUG
        # set saving state (if True, then it will save some maps to a folder when they can be analysed)
        args = sys.argv
        self.args = args
        self.is_saving = "--save" in args
        self.is_plotting = "--plot" in args
        self.saving_index = 0
        self.map_name = ""
        self.SAVE_TIME_CONSTANT = 10
        if self.is_plotting:
            self.live_vizualiser = ImageVizualiser()
            try:
                self.SAVE_TIME_CONSTANT = int(args[args.index("--plot")+1])
            except:
                pass
        if self.is_saving:
            idx = args.index("--save")
            self.map_name = args[idx + 1]
            print("Name : ", self.map_name)
            try:
                self.SAVE_TIME_CONSTANT = int(args[idx+2])
            except:
                pass
        print("Controller is ready: Is Ploting ? {}  - Is Saving ? {} - rate = {}".format(self.is_plotting, self.is_saving, self.SAVE_TIME_CONSTANT))

        # for debugging
        if "--travel" in args:
            self.start_travel_mode()

        if "--search" in args:
            self.state = RANDOM_SEARCH_MODE
            self.start_random_search_detection()

        if "--reach" in args:
            self.state = BOTTLE_REACHING_MODE
            #self.lidar_save_index = 0


        # STATE MACHINE
        # send a request for continuous rotation after waiting 1 second for UART node to be ready
        # todo: change '0' to '3' when launching controller1 within launch file
        time.sleep(2)
        if self.state == INITIAL_ROTATION_MODE:
            self.uart_publisher.publish(String(data = "r"))


    ### CALLBACKS
    # callbacks are the entry points to all other methods

    def listener_callback_map(self, map_message):
        if self.state == TRAVEL_MODE:
            self.travel_mode(map_message)

    def listener_callback_position(self, pos):
        """This function just receives the position and will update it to self variables.
        All control logics are in the 'map' calback"""
        # receive the position from the SLAM
        self.x = pos.x / 1200
        self.y = pos.y / 1200
        self.theta = pos.theta % 360

    def lidar_callback(self, msg):
        if "--travel" in self.args: 
            if self.robot_pos is None or self.zones is None or not len(self.robot_pos) or self.theta is None:
                return
            is_rock, angle = controller_utils.is_obstacle_a_rock(np.concatenate((self.robot_pos, [self.theta]), axis = 0), 
                    self.zones)

        if self.state == BOTTLE_REACHING_MODE:
            obstacle_detected = lidar_utils.check_obstacle_ahead(msg.distances, msg.angles, threshold_low = 15) 
            if obstacle_detected: 
                print("Obstacle detected AHEAD of lidar. Let's STOP. Bottle Picking Mode")
                self.uart_publisher.publish(String(data="x"))
                self.start_rotation_timer(DELTA_RANDOM_SEARCH, TIMER_STATE_ON_RANDOM_SEARCH_DELTA_ROTATION)

        elif self.state == TRAVEL_MODE and self.is_traveling_forward:
            print("checking with LIDAR")
            obstacle_detected = lidar_utils.check_obstacle_ahead(msg.distances, msg.angles, length_to_check = 350) 
            if obstacle_detected:
                print("Obstacle detected AHEAD of lidar. Let's STOP. Travel MODE")
                self.uart_publisher.publish(String(data="x"))
                self.has_to_find_new_path = True

        elif self.state == BOTTLE_RELEASE_MODE and self.is_traveling_forward:
            obstacle_detected = lidar_utils.check_obstacle_ahead(msg.distances, msg.angles, length_to_check = 700) 
            if obstacle_detected:
                print("Obstacle detected AHEAD of lidar. HOME DETECTED ! ")
                self.is_traveling_forward = False
                self.uart_publisher.publish(String(data="x"))
                self.uart_publisher.publish(String(data="q"))


    def listener_arduino_status(self, status_msg):
        """Called when Arduino send something to Jetson
        Messages type
        0: ERROR
        1: SUCESS
        2: IN PROGRESS
        """
        status = status_msg.status
        if self.state == INITIAL_ROTATION_MODE:
            if status == 1:
                print("* Initial Rotation Mode --> Travel Mode")
                self.start_travel_mode()

        elif self.state == BOTTLE_REACHING_MODE:
            if status == 0: 
                # = max distance reached
                print("Robot advanced maximum distance in 'y' mode")
                self.start_random_search_detection()
            elif status == 1:
                print("Robot finished reaching")
                # = there is a small obstacle ahead of the robot, lets pick it ! 
                self.state = BOTTLE_PICKING_MODE
                self.uart_publisher.publish(String(data="p"))

        elif self.state == BOTTLE_PICKING_MODE:
            if status == 0:
                # = no bottle were detected by the robot arm 
                self.start_random_search_detection()
            elif status == 1: # robot picked the bottle 
                print("Bottles picked: ", self.bottles_picked)
                self.bottles_picked += 1
                self.start_random_search_detection()

        elif self.state == BOTTLE_RELEASE_MODE:
            if status == 1:
                print("Release is finished")
                self.bottles_picked = 0
                self.n_random_search = 0
                self.start_travel_mode()

        elif self.state == RECOVERY_SLAM:
            if status == 1:
                print("Slam recovery arduino")
                # we assume that arduino could handle properly the rescue
                self.slam_control_publisher.publish(String(data="recover_state"))
                # go back to travel mode
                self.start_travel_mode()

        elif self.state == RECOVERY_ROTATION:
            if status == 1:
                # robot moved foward. 
                # we must start again the rotation with was unsucessful
                self.state = self.last_state
                print("Rotation recovery arduino")
                self.start_rotation_timer(self.rotation_asked, self.rotation_timer_state)

        elif self.state == KICK_ASS_MODE:
            if status == 1:
                print("[Arduino says]: we start the approach")
                # robot reached the bottles and is ready to start the KICK ASS BACK ATTACK
                self.slam_control_publisher.publish(String(data="freeze"))
                self.uart_publisher.publish(String(data="c"))
            if status == 3: 
                print("[Arduino wonders]: mission successful (?)")
                # robot has finished the kick ass mode.
                # try to create SLAM again
                self.slam_control_publisher.publish(String(data="unfreeze"))
            if status == 4:
                ##### self.start_rotation_timer(1, TIMER_STATE_ON_NO_ROTATION)
                # = SLAM has waited and it is now time to start again the random search
                print("[Arduino says]: Waiting time is finished, SLAM ready to go")
                dest = TARGETS_TO_VISIT[self.current_target_index]
                is_going_home = dest == 0
                if is_going_home:
                    print("going home")
                    self.start_travel_mode()
                else:
                    print("starting random seach inside rocks")
                    self.n_random_search = N_RANDOM_SEARCH_MAX - 10
                    self.bottles_picked = MAX_BOTTLE_PICKED - 3
                    self.start_random_search_detection()



    def listener_callback_detectnet(self, msg):
        """Called when a bottle is detected by neuron network
        This function can only be called when the neuron network is active, 
        i.e. only in RANDOM_SEARCH_MODE when the robot is still and waiting for detection
        """
        # we must verify that the detectnet is really suppose to be turned ON
        if self.detectnet_state == DETECTNET_OFF: 
            return 
        # we must verify that actual flip state is the same as expected flip state
        source_img = msg.detections[0].source_img
        is_actually_flipped = source_img.height < source_img.width
        if is_actually_flipped != self.is_flipped:
            return

        # 1. extract the detection
        print("    Detections successful")
        new_detections = [(d.bbox.center.x, d.bbox.center.y, d.bbox.size_x, d.bbox.size_y, self.is_flipped) for d in msg.detections]
        print(new_detections, msg.detections[0].source_img.width)
        self.detections += new_detections

        # 2. flip the camera
        self.flip_camera_and_reset_detectnet_timer()

    def detection_timer_callback(self):
        """Timer called after 2 seconds and destroyed immeditaly when a bottle is detected.
        If the calback is called, it means no bottle were detected during its period.
        """
        print("    No bottle detected during time interval")
        self.flip_camera_and_reset_detectnet_timer()

    def flip_camera_and_reset_detectnet_timer(self):
        msg = "normal" if self.is_flipped else "flip"
        self.camera_flip_topic.publish(String(data=msg))
        self.destroy_timer(self.wait_for_detectnet_timer)
        self.is_flipped = not self.is_flipped
        if self.is_flipped:
            # = first lap is finished 
            # create a callback in some time to observe bottles around robot
            print("    Trying to detect again with a new flip")
            self.wait_for_detectnet_timer = self.create_timer(TIME_FOR_VISION_DETECTION, self.detection_timer_callback)
        else:
            # = nothing was detected during the second lap
            # get the best bottle to go to
            self.take_bottle_decision()

    def take_bottle_decision(self):
        """Given the detections, find best action to do 
        - a bottle to pick 
        - another rotation
        """
        self.set_detectnet_state(DETECTNET_OFF)
        if len(self.detections):
            # get best detection
            detection = vision_utils.get_best_detections(self.detections)
            # move to bottle
            angle = vision_utils.get_angle_of_detection(detection)
            print("starting timer after detection of bottle, with angle:",angle)
            self.start_rotation_timer(angle, TIMER_STATE_ON_RANDOM_SEARCH_BOTTLE_ALIGNMENT)
        else:
            # lets start a rotation of 30 degrees again
            print("    No bottle detected at all --> start again a rotation")
            self.start_rotation_timer(DELTA_RANDOM_SEARCH, TIMER_STATE_ON_RANDOM_SEARCH_DELTA_ROTATION)

    def rotation_timer_callback(self):
        """Called when robot has turned enough to pick the bottle"""
        print("ROTATION CALLBACK ", self.rotation_index)
        self.destroy_timer(self.rotation_timer)

        if self.rotation_timer_state == TIMER_STATE_OFF:
            print("Timer was OFF and yet trigered")

        # verify that rotation actually happened
        if np.abs(self.rotation_asked) > 39:
            if np.abs(controller_utils.angle_diff(self.last_theta, self.theta)) < 5:
                print("ROTATION ERROR ! index: ", self.rotation_index)
                print(self.last_theta)
                print(self.theta)
                print(controller_utils.angle_diff(self.last_theta, self.theta))
                # ask arduino to move forward (just a little bit) and wait for answer
                self.uart_publisher.publish(String(data="W"))
                self.last_state = self.state
                self.state = RECOVERY_ROTATION
                return 

        if self.rotation_timer_state == TIMER_STATE_ON_RANDOM_SEARCH_BOTTLE_ALIGNMENT:
            print("    Robot is in front of bottle")
            # change timer state and go to bottle picking mode.
            self.rotation_timer_state = TIMER_STATE_OFF
            self.start_bottle_reaching_mode()

        elif self.rotation_timer_state == TIMER_STATE_ON_RANDOM_SEARCH_DELTA_ROTATION:
            print("    Robot delta rotation finished")
            self.rotation_timer_state = TIMER_STATE_OFF
            self.uart_publisher.publish(String(data="x"))
            # start detection again
            self.start_random_search_detection()

        elif self.rotation_timer_state == TIMER_STATE_ON_TRAVEL_MODE:
            # change timer state and start moving forward.
            self.rotation_timer_state = TIMER_STATE_OFF
            print("    Rotated time reached. Let's move forward.")
            self.uart_publisher.publish(String(data="w"))

        elif self.rotation_timer_state == TIMER_STATE_ON_BOTTLE_RELEASE:
            # = robot is aligned with the recycling area
            print("Ready to move forward")
            self.rotation_timer_state = TIMER_STATE_OFF
            self.is_traveling_forward = True
            self.uart_publisher.publish(String(data="m2"))
            self.uart_publisher.publish(String(data="w"))

        elif self.rotation_timer_state == TIMER_STATE_ON_NO_ROTATION:
            print("Waiting time finished.")

        elif self.rotation_timer_state == TIMER_STATE_ON_KICK_ASS_MODE: 
            print("We are ready Arduino ! Take care of us.... (Sending 'Y')")
            self.rotation_timer_state = TIMER_STATE_OFF
            # 2. start communication with Arduino 
            # the continuation is in arduino callback
            self.uart_publisher.publish(String(data="Y"))

        elif self.rotation_timer_state == TIMER_STATE_ON_TRAVEL_MODE_END:
            self.rotation_timer_state = TIMER_STATE_OFF
            self.start_random_search_detection()

    ### STATE MACHINE METHODS

    def set_detectnet_state(self, new_state):
        self.detectnet_state = new_state
        if new_state == DETECTNET_ON:
            self.cam_publisher.publish(String(data="create"))
        if new_state == DETECTNET_OFF:
            self.cam_publisher.publish(String(data="destroy"))

    def start_random_search_detection(self):
        """Will start the random search and increase by 1 the stepper
        """
        print("* Random search activated, n = ", self.n_random_search)
        self.state = RANDOM_SEARCH_MODE
        self.n_random_search += 1

        # ending criterion 
        has_to_stop_search = (self.n_random_search == N_RANDOM_SEARCH_MAX) or (self.bottles_picked == MAX_BOTTLE_PICKED)
        if has_to_stop_search:
            print("Leaving random search")
            # no more random walk can happen
            # let's enter travel mode again
            self.set_detectnet_state(DETECTNET_OFF)
            self.start_travel_mode()
            return

        # set lower speed
        self.uart_publisher.publish(String(data = "xm2"))

        # create subscription for detection
        self.set_detectnet_state(DETECTNET_ON)

        # create a callback in some time to observe bottles around robot
        self.detections = []
        self.wait_for_detectnet_timer = self.create_timer(TIME_FOR_VISION_DETECTION, self.detection_timer_callback)

    def travel_mode(self, map_message):
        """Travel mode of the controller.
        This function is called by the map listener's callback.
        """
        # compute robot position (used a lot)
        self.robot_pos = map_utils.pos_to_gridpos(self.x, self.y)

        if self.rotation_timer_state == TIMER_STATE_ON_TRAVEL_MODE_END:
            return 

        ### I. Path planning
        # Once in a while, start the path planning logic
        if int(map_message.index) % CONTROLLER_TIME_CONSTANT == 0 or self.has_to_find_new_path:
            print("    map analysis", int(map_message.index))

            ## Handling timer problem
            if self.rotation_timer_state == TIMER_STATE_ON_TRAVEL_MODE:
                print("Stopping current timer and let's compute a new path to follow")
                self.uart_publisher.publish(String(data = "x"))
                self.rotation_timer_state = TIMER_STATE_OFF
                self.destroy_timer(self.rotation_timer)

            ## Map analysis
            # a. filter the map
            map_data = bytearray(map_message.map_data)
            m = map_utils.get_map(map_data)
            binary_dilated, binary = map_utils.filter_map(m, dilation_kernel_size = 14)
            
            # np.save("/home/arthur/dev/ros/data/maps/test30m.npy", m)

            # b. get rectangle around the map
            try:
                corners, area, contours = map_utils.get_bounding_rect(binary)
            except:
                print("Contours not found... yet ?")
                return

            # save binary if we are going to make some plots
            if self.is_saving or self.is_plotting:
                self.binary = binary_dilated
                self.contours = contours
                self.corners = corners

            # c. find zones
            # zones are ordered the following way: (recycling area, zone2, zone3, zone4)
            if not self.initial_zones_found and area > AREA_THRESHOLD:
                if area > 240000:
                    raise RuntimeError("Zones were not found properly")

               # corners found are valid and we can find the 'initial zones'
                self.zones = map_utils.get_initial_zones(corners, self.robot_pos, closest_zone = 0)
                self.initial_zones_found = True
                print("    - initial zones found with area: ", area)


            if self.initial_zones_found:
                # update zones with new map
                new_zones = map_utils.get_zones_from_previous(corners, self.zones)
                are_valid = map_utils.are_new_zones_valid(new_zones, self.zones)
                if are_valid:
                    self.slam_control_publisher.publish(String(data="save_state"))
                    self.zones = new_zones
                else:
                    print("RECOVERY MODE STARTED")
                    self.state = RECOVERY_SLAM
                    self.uart_publisher.publish(String(data="R"))
                    return 

                ## Path Planing
                # d. get targets positions for each zones
                self.targets = map_utils.get_targets_from_zones(np.array(self.zones))

                # e. rrt_star path planning
                self.goal = self.targets[TARGETS_TO_VISIT[self.current_target_index]]
                random_area = map_utils.get_random_area(self.zones)
                print("    - will find path")
                rrt = RRTStar(start = self.robot_pos, goal = self.goal, binary_obstacle = binary_dilated, 
                        rand_area = random_area, expand_dis = 50, path_resolution = 1,
                        goal_sample_rate = 5, max_iter = 500)
                self.path = np.array(rrt.planning(animation = True))
                self.has_to_find_new_path = False
                print("    - path found")

        # (make and save the nice figure)
        if (self.is_saving or self.is_plotting) and int(map_message.index) % self.SAVE_TIME_CONSTANT == 0:
            name = self.map_name+str(self.saving_index)
            save_name = "/home/arthur/dev/ros/data/maps/rects/"+name+".png" if self.is_saving else ""
            text = ""
            try:
                img = map_utils.make_nice_plot(self.binary, save_name, self.robot_pos,
                        self.theta, self.contours, self.corners,
                        self.zones, self.targets, self.path.astype(int),
                        text = text)
                if self.is_plotting:
                    self.live_vizualiser.display(np.array(img))
                print("-----> saving index: ", self.saving_index, int(map_message.index))
                self.saving_index += 1
                # np.save("/home/arthur/dev/ros/data/maps/"+name+".npy", m)
            except:
                print("Could not save")


        ### II. Path Tracking
        # 0. end condition
        if self.path is None: 
            self.has_to_find_new_path = True
            print("NO PATH FOUND.....")
            return 

        if len(self.path) == 0 or self.goal is None: 
            print("...")
            return

        # 1. state transition condition
        dist = controller_utils.get_distance(self.robot_pos, self.goal)
        reached = TARGETS_TO_VISIT[self.current_target_index]
        min_dist = MIN_DIST_TO_RECYCLING if reached == 0 else MIN_DIST_TO_GOAL
        if dist < min_dist:
            # robot arrived to destination
            print("Robot reached zone ", reached)
            self.current_target_index += 1
            self.n_random_search = 0
            self.bottles_picked = 0
            self.is_traveling_forward = False
            self.path = []
            if reached in [1,2,3,4]: # robot in zone 2 or zone 3
                # travel_mode --> random_search mode
                line_orientation = controller_utils.get_path_orientation([self.zones[1], self.zones[0]] if reached in [1, 2] else [self.zones[3], self.zones[1]])
                angle_diff = controller_utils.angle_diff(line_orientation, self.theta)
                self.start_rotation_timer(angle_diff, TIMER_STATE_ON_TRAVEL_MODE_END)
            elif reached == 0:
                # travel_mode --> release_bottle_mode
                self.start_bottle_release_mode()
            elif reached == 5 or reached == 6:
                # travel_mode --> ROCKS KICK ASS MODE
                self.start_kick_ass_mode(is_going_home = reached == 6)
            return

        # 2. Else, compute motors commands
        if self.rotation_timer_state == TIMER_STATE_ON_TRAVEL_MODE: 
            return

        path_orientation = controller_utils.get_path_orientation(self.path)
        diff = controller_utils.angle_diff(path_orientation, self.theta)

        if abs(diff) > MIN_ANGLE_DIFF:
            ## ROTATION CORRECTION SUB-STATE
            print("    rotation correction with diff = ", diff)
            self.is_traveling_forward = False
            self.start_rotation_timer(diff, TIMER_STATE_ON_TRAVEL_MODE)

        else:
            ## FORWARD SUB-STATE
            # in theory, robot should be going forward.
            # send a forward message just in case it wasn't lunched before
            self.uart_publisher.publish(String(data = "w"))
            self.is_traveling_forward = True
            # compute distance to next point of the path
            p = self.path[-2]
            dist_to_next_point = controller_utils.get_distance(self.robot_pos, p)
            print("    going foward for a distance {:.2f}, diff = {:.2f}".format(dist_to_next_point, diff))
            if dist_to_next_point < MIN_DIST_TO_POINT:
                # remove first point of the path
                print("Will update path: ", self.path)
                del self.path[-1]
                print("Updated path: ", self.path)

    def start_bottle_reaching_mode(self):
        """Will start the bottle picking mode"""
        print("Robot starts bottle reaching mode")
        self.state = BOTTLE_REACHING_MODE
        self.uart_publisher.publish(String(data = "y"))

    def start_travel_mode(self):
        self.uart_publisher.publish(String(data = "m1"))
        self.has_to_find_new_path = True
        self.state = TRAVEL_MODE

    def start_bottle_release_mode(self):
        """Will start the bottle picking mode"""
        self.state = BOTTLE_RELEASE_MODE
        # 1. get angle to rotate to align robot to correct position
        diagonal_orientation = controller_utils.get_path_orientation([self.zones[0], self.zones[3]])
        angle = controller_utils.angle_diff(diagonal_orientation, self.theta)
        print("BOTTLE RELEASE with angle diff: ", angle, "values ", self.theta, diagonal_orientation)
        # 2. make the rotation
        self.start_rotation_timer(angle, TIMER_STATE_ON_BOTTLE_RELEASE)

    def start_kick_ass_mode(self, is_going_home):
        print("Kick the Rocks Asses")
        self.state = KICK_ASS_MODE 
        # 1. find the angle to rotate the robot at
        line_orientation = controller_utils.get_path_orientation([self.zones[0], self.zones[2]] if is_going_home else [self.zones[2], self.zones[0]])
        angle = controller_utils.angle_diff(line_orientation, self.theta)
        self.start_rotation_timer(angle, TIMER_STATE_ON_KICK_ASS_MODE)


    ### HELPER FUNCTIONS

    def start_rotation_timer(self, angle, state):
        """Will start a timer which has a period equals to the required rotation time
        to achieve the provided angle."""

        # 1. if required, delete previous timer
        if self.rotation_timer_state is not TIMER_STATE_OFF:
            # it means another timer was launched
            self.destroy_timer(self.rotation_timer)

        if self.rotation_timer_state == TIMER_STATE_ON_NO_ROTATION:
            # This is a 'fake' state to wait for an amount of time without doing anything
            time_to_rotate = angle 
        else:
            # 2. estimate remaining time of rotation and start new timer
            time_to_rotate = controller_utils.get_rotation_time(np.abs(angle))
            # 3. send the rotation motor control
            print("        (starting rotation now)", state, angle, self.rotation_index)
            msg = String()
            if angle > 0: msg.data = "d"
            else: msg.data = "a"
            self.uart_publisher.publish(msg)

        # launch the timer
        self.rotation_timer = self.create_timer(time_to_rotate, self.rotation_timer_callback)
        
        # 4. get current state of the robot (to make sure a real rotation happened)
        self.rotation_timer_state = state
        self.last_theta = self.theta
        self.rotation_asked = angle
        self.rotation_index += 1

    def log_line(self, msg):
        print("---------------------------")

def main(args=None):
    rclpy.init(args=args)
    node = Controller1()
    rclpy.spin(node)
    print("Leaving code !")
    rclpy.shutdown()

if __name__ == '__main__':
    main()
