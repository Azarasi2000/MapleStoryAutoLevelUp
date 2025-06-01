import time
import cv2
import numpy as np
import pydirectinput
import threading
from windows_capture import WindowsCapture, Frame, InternalCaptureControl
import keyboard
import pygetwindow as gw
import random
import argparse

# local import
from config import Config
from logger import logger

# Do not modify the following parameters
# color code for patrol route
COLOR_CODE = {
    # R   G   B
    (255, 0, 0): "walk left", # red
    (0, 0, 255): "walk right", # blue
    (255,127,0): "jump left", # orange
    (0,255,255): "jump right", # sky blue
    (255,0,255): "jump", # purple
    (127,127,127): "up", # gray
    (0,255,0): "stop", # green
    (255,255,0): "goal", # yellow
}
GAME_WINDOW_TITLE = 'MapleStory Worlds-Artale (繁體中文版)'

# Global Variables
IS_RUN = True
FAKE_KEYBOARD_COMMAND = ""
FRAME_BUFFER = None
CFG = Config # Custumized configuration
LOCK_FRAME_BUFFER = threading.Lock()
STATUS = "hunting" # 'resting', 'finding_rune', 'near_rune', 'solving_rune'
T_LAST_SWITCH_STATUS = time.time() # timestamp when status switches last time
IS_DEBUG_MONSTER = False # option to enable output monster debuging window

# Create game screen capture session 
capture = WindowsCapture(window_name=GAME_WINDOW_TITLE)

@capture.event
def on_frame_arrived(frame: Frame, capture_control: InternalCaptureControl):
    '''
    on_frame_arrived
    '''
    global FRAME_BUFFER
    with LOCK_FRAME_BUFFER:
        FRAME_BUFFER = frame.frame_buffer
    time.sleep(0.033) # Cap FPS to 30

# Capture closed callback
@capture.event
def on_closed():
    '''
    on_closed
    '''
    logger.info("Capture session closed.")
    cv2.destroyAllWindows()

def is_game_window_active():
    '''
    Check if the game window is currently the active (foreground) window.

    Returns:
    - True
    - False
    '''
    active_window = gw.getActiveWindow()
    return active_window is not None and GAME_WINDOW_TITLE in active_window.title

def find_nearest_color_code(player_loc, img_route):
    '''
    Search for the nearest valid color code around the player's location.

    The function searches within a square window (defined by COLOR_CODE_SEARCH_RANGE)
    around the given player location, and finds the nearest pixel whose color matches
    any of the predefined COLOR_CODE keys.

    Parameters:
    - player_loc: tuple (x, y), the current player position.
    - img_route: image (numpy array, shape HxWx3), the full route map image.

    Returns:
    - nearest: dict containing information of the nearest color code found:
        {
            "pixel": (x, y),        # pixel coordinate
            "color": (R, G, B),     # matched color
            "action": COLOR_CODE[], # corresponding action
            "distance": dist        # Manhattan distance to player
        }
      or None if no matching color code is found.
    '''
    x0, y0 = player_loc
    h, w = img_route.shape[:2]  # Get image height and width

    x_min = max(0, x0 - CFG.COLOR_CODE_SEARCH_RANGE)
    x_max = min(w, x0 + CFG.COLOR_CODE_SEARCH_RANGE)
    y_min = max(0, y0 - CFG.COLOR_CODE_SEARCH_RANGE)
    y_max = min(h, y0 + CFG.COLOR_CODE_SEARCH_RANGE)

    nearest = None
    min_dist = float('inf')
    for y in range(y_min, y_max):
        for x in range(x_min, x_max):
            pixel = tuple(img_route[y, x])  # (R, G, B)
            if pixel in COLOR_CODE:
                dist = abs(x - x0) + abs(y - y0)
                if dist < min_dist:
                    min_dist = dist
                    nearest = {
                        "pixel": (x, y),
                        "color": pixel,
                        "action": COLOR_CODE[pixel],
                        "distance": dist
                    }

    return nearest  # if not found return none

def nms(monsters, iou_threshold=0.3):
    '''
    Apply Non-Maximum Suppression (NMS) to remove overlapping detections.

    Parameters:
    - monsters: List of dictionaries, each representing a detected monster with:
        - "position": (x, y) top-left corner
        - "size": (width, height)
        - "score": similarity/confidence score from template matching
    - iou_threshold: Float, intersection-over-union threshold to suppress overlapping boxes

    Returns:
    - List of filtered monster dictionaries after applying NMS
    '''
    boxes = []
    for m in monsters:
        x, y = m["position"]
        w, h = m["size"]
        # [x1, y1, x2, y2, score, original_data]
        boxes.append([x, y, x + w, y + h, m["score"], m])

    # Sort by score descending
    boxes.sort(key=lambda x: x[4], reverse=True)

    keep = []
    while boxes:
        best = boxes.pop(0)
        keep.append(best[5])  # original monster_info

        boxes = [b for b in boxes if get_iou(best, b) < iou_threshold]

    return keep

def get_iou(box1, box2):
    '''
    Calculate the Intersection over Union (IoU) between two bounding boxes.

    Each box is expected to be a tuple or list with at least 4 values:
    (x1, y1, x2, y2), where:
        - (x1, y1) is the top-left corner
        - (x2, y2) is the bottom-right corner

    Returns:
        A float representing the IoU value (0.0 ~ 1.0).
        If there is no overlap, returns 0.0.
    '''
    x1, y1, x2, y2 = box1[:4]
    x1_p, y1_p, x2_p, y2_p = box2[:4]

    inter_x1 = max(x1, x1_p)
    inter_y1 = max(y1, y1_p)
    inter_x2 = min(x2, x2_p)
    inter_y2 = min(y2, y2_p)

    if inter_x2 <= inter_x1 or inter_y2 <= inter_y1:
        return 0.0

    inter_area = (inter_x2 - inter_x1) * (inter_y2 - inter_y1)
    area1 = (x2 - x1) * (y2 - y1)
    area2 = (x2_p - x1_p) * (y2_p - y1_p)
    union = area1 + area2 - inter_area

    return inter_area / union

def is_monster_in_range(
        monster_info,
        attack_box_top_left,
        overlap_threshold=0.5):
    '''
    Check if any monster's box overlaps with the player's attack box
    by at least a certain threshold (default is 50%).

    Parameters:
    - monster_info: list of dicts, each containing monster's "position" and "size"
    - attack_box_top_left: tuple (x, y), top-left corner of the attack range box
    - overlap_threshold: float, minimum overlap ratio (intersection / monster area)
                         to consider a attackable monster

    Returns:
    - True if any monster has sufficient overlap with attack box, False otherwise
    '''
    ax1, ay1 = attack_box_top_left
    ax2, ay2 = (attack_box_top_left[0] + CFG.MAGIC_CLAW_RANGE_X,
                attack_box_top_left[1] + CFG.MAGIC_CLAW_RANGE_Y)

    for monster in monster_info:
        mx1, my1 = monster["position"]
        mw, mh = monster["size"]
        mx2 = mx1 + mw
        my2 = my1 + mh

        # Calculate intersection
        ix1 = max(ax1, mx1)
        iy1 = max(ay1, my1)
        ix2 = min(ax2, mx2)
        iy2 = min(ay2, my2)

        iw = max(0, ix2 - ix1)
        ih = max(0, iy2 - iy1)
        inter_area = iw * ih

        monster_area = mw * mh
        if monster_area == 0:
            continue  # skip degenerate box

        overlap_ratio = inter_area / monster_area

        if overlap_ratio >= overlap_threshold:
            return True  # found monster within range

    return False

def find_pattern(
        img,
        img_pattern,
        last_result=None,
        mask=None,
        local_search_radius=50,
        global_threshold=0.6):
    '''
    Locate the most possible pattern in a larger image
    It first attempts a fast local search around the previous result;
    if it fails, it falls back to a global search.

    Parameters:
    - img: The target image (numpy array) as search space.
    - img_pattern: The smaller pattern/template image to search for.
    - last_result: Tuple (x, y) of the previous pattern location.
                   If provided, enables local search.
    - local_search_radius: window size (in pixels) to perform local search.
    - global_threshold: Match threshold to accept local match;
                        otherwise fallback to full image search.

    Returns:
    - top_left: (x, y) top-left corner coordinate of the best matching.
    '''
    # Get mask
    # _, mask_pattern = cv2.threshold(img_pattern, 1, 255, cv2.THRESH_BINARY)

    h, w = img_pattern.shape[:2]

    if last_result is not None:
        lx, ly = last_result
        x1 = max(0, lx - local_search_radius)
        y1 = max(0, ly - local_search_radius)
        x2 = min(img.shape[1], lx + local_search_radius + w)
        y2 = min(img.shape[0], ly + local_search_radius + h)

        # Local search
        img_local = img[y1:y2, x1:x2]
        # res_local = cv2.matchTemplate(img_local, img_pattern, cv2.TM_CCOEFF_NORMED, mask=mask_pattern)
        if  mask is None:
            res_local = cv2.matchTemplate(img_local, img_pattern, cv2.TM_CCOEFF_NORMED)
        else:
            res_local = cv2.matchTemplate(img_local, img_pattern, cv2.TM_CCOEFF_NORMED, mask=mask)
        min_val, max_val, min_loc, max_loc = cv2.minMaxLoc(res_local)

        if max_val >= global_threshold:
            top_left = (x1 + max_loc[0], y1 + max_loc[1])
            return top_left, max_val

    # Global fallback
    # res = cv2.matchTemplate(img, img_pattern, cv2.TM_CCOEFF_NORMED, mask=mask_pattern)
    res = cv2.matchTemplate(img, img_pattern, cv2.TM_CCOEFF_NORMED)
    if mask is None:
        res = cv2.matchTemplate(img, img_pattern, cv2.TM_CCOEFF_NORMED)
    else:
        res = cv2.matchTemplate(img, img_pattern, cv2.TM_CCOEFF_NORMED, mask=mask)
    min_val, max_val, min_loc, top_left = cv2.minMaxLoc(res)

    return top_left, max_val

def screenshot(img):
    '''
    Save the given image as a screenshot file.

    Parameters:
    - img: numpy array (image to save).

    Behavior:
    - Saves the image to the "screenshot/" directory with the current timestamp as filename.
    '''
    filename = f"screenshot/screenshot_{int(time.time())}.png"
    cv2.imwrite(filename, img)
    logger.info(f"Screenshot saved: {filename}")

loc_last = (0, 0)
t_last_move = time.time()
def is_stuck(loc):
    """
    Detect if the player is stuck (not moving).
    If stuck for more than WATCH_DOG_TIMEOUT seconds, performs a random action.
    """
    global loc_last, t_last_move

    current_time = time.time()

    dx = abs(loc[0] - loc_last[0])
    dy = abs(loc[1] - loc_last[1])

    if dx + dy > CFG.WATCH_DOG_RANGE:
        # Player moved, reset
        loc_last = loc
        t_last_move = current_time
        return False

    if current_time - t_last_move > CFG.WATCH_DOG_TIMEOUT:
        # Stuck too long
        loc_last = loc
        t_last_move = current_time
        return True

    return False

def press_key(key, duration):
    '''
    Simulates a key press for a specified duration using pydirectinput.
    '''
    pydirectinput.keyDown(key)
    time.sleep(duration)
    pydirectinput.keyUp(key)

def fake_keyboard_input_worker():
    '''
    Continuously listens to the global FAKE_KEYBOARD_COMMAND
    and simulates key inputs using pydirectinput.
    '''
    # store last 'up' command time
    t_last_up = 0.0

    while True:
        # Check if game window is active
        if not is_game_window_active():
            time.sleep(0.001)
            continue

        # If solving rune, skip all keyboard command
        if STATUS == "solving_rune":
            time.sleep(0.001)
            continue

        # check if is needed to release 'Up' key
        if time.time() - t_last_up > CFG.UP_DRAG_DURATION:
            keyboard.release("up")

        if FAKE_KEYBOARD_COMMAND == "walk left":
            keyboard.release("right")
            keyboard.press("left")

        elif FAKE_KEYBOARD_COMMAND == "walk right":
            keyboard.release("left")
            keyboard.press("right")

        elif FAKE_KEYBOARD_COMMAND == "jump left":
            keyboard.release("right")
            keyboard.press("left")
            press_key(CFG.JUMP_KEY, 0.02)
            keyboard.release("left")

        elif FAKE_KEYBOARD_COMMAND == "jump right":
            keyboard.release("left")
            keyboard.press("right")
            press_key(CFG.JUMP_KEY, 0.02)
            keyboard.release("right")

        elif FAKE_KEYBOARD_COMMAND == "jump":
            keyboard.release("left")
            keyboard.release("right")
            press_key(CFG.JUMP_KEY, 0.02)

        elif FAKE_KEYBOARD_COMMAND == "up":
            keyboard.press("up")
            t_last_up = time.time()

        elif FAKE_KEYBOARD_COMMAND == "attack left":
            keyboard.release("right")
            keyboard.press("left")
            press_key(CFG.ATTACK_KEY, 0.02)
            keyboard.release("left")

        elif FAKE_KEYBOARD_COMMAND == "attack right":
            keyboard.release("left")
            keyboard.press("right")
            press_key(CFG.ATTACK_KEY, 0.02)
            keyboard.release("right")

        elif FAKE_KEYBOARD_COMMAND == "stop":
            keyboard.release("left")
            keyboard.release("right")
            keyboard.release("up")

        else:
            # Release all keys, stop the character
            keyboard.release("left")
            keyboard.release("right")

        if STATUS == "near_rune":
            # keep smashing 'up' key
            press_key("up", 0.02)

        time.sleep(0.001)

def draw_rectangle(img, top_left, size, color, text):
    '''
    Draws a rectangle with an text label.

    Parameters:
    - img: The image on which to draw (numpy array).logger
    - top_left: Tuple (x, y), the top-left corner of the rectangle.
    - size: Tuple (height, width) of the rectangle.
    - color: Tuple (B, G, R), color of the rectangle and text.
    - text: String to display above the rectangle.
    '''
    bottom_right = (top_left[0] + size[1],
                    top_left[1] + size[0])
    cv2.rectangle(img, top_left, bottom_right, color, 2)
    cv2.putText(img, text, (top_left[0], top_left[1] - 10),
                cv2.FONT_HERSHEY_SIMPLEX, 0.5, color, 1)

def switch_status(new_status):
    '''
    Switch to new status and log the transition.

    Parameters:
    - new_status: string, the new status to switch to.
    '''
    global STATUS, T_LAST_SWITCH_STATUS

    t_elapsed = round(time.time() - T_LAST_SWITCH_STATUS)
    logger.info(f"[switch_status] From {STATUS}({t_elapsed} sec) to {new_status}.")
    STATUS = new_status
    T_LAST_SWITCH_STATUS = time.time()

def solve_rune(img, img_arrows):
    '''
    Solve the rune puzzle by detecting the arrow directions and pressing corresponding keys.

    Parameters:
    - img: The full game screen image (numpy array).
    - img_arrows: Dictionary containing arrow templates for each direction, 
                  formatted as {direction: [list of template images]}.

    Behavior:
    - For each arrow box position, extract the region of interest.
    - Match the ROI against all arrow templates using template matching (SQDIFF).
    - Select the arrow direction with the best (lowest) score.
    - Log the detected direction and score.
    - Press the corresponding direction key.
    - Take a screenshot after each solved arrow.
    - Wait 2 seconds between key presses.
    - Logs when all arrows have been solved.
    '''
    for arrow_idx in [0,1,2,3]:
        # Crop arrow detection box
        x = CFG.ARROW_BOX_START_POINT[0] + CFG.ARROW_BOX_INTERVAL*arrow_idx
        y = CFG.ARROW_BOX_START_POINT[1]
        size = CFG.ARROW_BOX_SIZE
        img_roi = img[y:y+size, x:x+size]

        # Loop through all possible arrows template and choose the most possible one
        best_score = float('inf')
        best_direction = ""
        for direction, arrow_list in img_arrows.items():
            for img_arrow in arrow_list:
                _, score = find_pattern_sqdiff(img_roi, img_arrow)
                if score < best_score:
                    best_score = score
                    best_direction = direction
        logger.info(f"[solve_rune] Arrow({arrow_idx}) is {best_direction} with score({best_score})")

        # Press the key
        press_key(best_direction, 0.5)
        time.sleep(2)

    logger.info(f"[solve_rune] Solved all arrows")

def find_pattern_sqdiff(img, img_pattern):
    '''
    Perform masked template matching using SQDIFF_NORMED method.

    The function searches for the best matching location of img_pattern inside img.
    It automatically converts the pattern to grayscale and generates a mask to ignore
    pure white (or near-white) pixels in the template, treating them as transparent background.

    Parameters:
    - img: Target search image (numpy array), can be grayscale or BGR.
    - img_pattern: Template image to search for (numpy array, BGR).

    Returns:
    - min_loc: The top-left coordinate (x, y) of the best match position.
    - min_val: The matching score (lower = better for SQDIFF_NORMED).
    '''
    # Convert template to grayscale
    img_pattern_gray = cv2.cvtColor(img_pattern, cv2.COLOR_BGR2GRAY)

    # Create mask: ignore pure white pixels (treat near-white as background)
    _, mask_pattern = cv2.threshold(img_pattern_gray, 254, 255, cv2.THRESH_BINARY_INV)

    # Perform template matching
    res = cv2.matchTemplate(img, img_pattern, cv2.TM_SQDIFF_NORMED, mask=mask_pattern)

    # Extract best match (min_val is better for SQDIFF)
    min_val, max_val, min_loc, max_loc = cv2.minMaxLoc(res)

    return min_loc, min_val

def main(args):
    '''
    Main function
    '''
    global FAKE_KEYBOARD_COMMAND, STATUS

    # Load images
    img_nametag = cv2.imread("nameTag.png", cv2.IMREAD_GRAYSCALE)
    img_map     = cv2.imread(f"maps/{args.map}/map.png", cv2.IMREAD_GRAYSCALE)
    img_routes = [
        cv2.cvtColor(cv2.imread(f"maps/{args.map}/route1.png"), cv2.COLOR_BGR2RGB),
        cv2.cvtColor(cv2.imread(f"maps/{args.map}/route2.png"), cv2.COLOR_BGR2RGB)
    ]
    img_please_remove_runes = cv2.imread("please_remove_runes.png", cv2.IMREAD_GRAYSCALE)
    img_rune = cv2.imread("rune/rune.png")
    img_arrows = {
        "left":
            [cv2.imread("rune/arrow_left_1.png"),
             cv2.imread("rune/arrow_left_2.png"),
             cv2.imread("rune/arrow_left_3.png"),],
        "right":
            [cv2.imread("rune/arrow_right_1.png"),
             cv2.imread("rune/arrow_right_2.png"),
             cv2.imread("rune/arrow_right_3.png"),],
        "up":
            [cv2.imread("rune/arrow_up_1.png"),
             cv2.imread("rune/arrow_up_2.png"),
             cv2.imread("rune/arrow_up_3.png")],
        "down":
            [cv2.imread("rune/arrow_down_1.png"),
             cv2.imread("rune/arrow_down_2.png"),
             cv2.imread("rune/arrow_down_3.png"),],
    }

    ALL_MONSTER_DICT = {
        'green_mushroom':
            [cv2.imread("monster/green_mushroom.png")],
        'spike_mushroom':
            [cv2.imread("monster/spike_mushroom.png")],
        'zombie_mushroom':
            [cv2.imread("monster/zombie_mushroom_1.png"),
             cv2.imread("monster/zombie_mushroom_2.png"),
             cv2.imread("monster/zombie_mushroom_3.png"),],
        'fire_pig':
            [cv2.imread("monster/fire_pig_1.png"),
             cv2.imread("monster/fire_pig_2.png"),
             cv2.imread("monster/fire_pig_3.png")],
        'black_axe_stump':
            [cv2.imread("monster/black_axe_stump_1.png"),
             cv2.imread("monster/black_axe_stump_2.png")],
    }

    # Add flipped monster image to ALL_MONSTER_DICT
    for monster_name, img_list in ALL_MONSTER_DICT.items():
        imgs = []
        for img in img_list:
            imgs.append(img)  # original
            imgs.append(cv2.flip(img, 1))  # flipped
        ALL_MONSTER_DICT[monster_name] = imgs

    # Select monsters based on args
    if args.monsters == "all":
        monster_dict = ALL_MONSTER_DICT
    else:
        selected = args.monsters.split(",")
        monster_dict = {k: v for k, v in ALL_MONSTER_DICT.items() if k in selected}
    logger.info(f"Loaded monsters: {list(monster_dict.keys())}")

    # Start fake keyboard input thread
    if not args.disable_control:
        threading.Thread(target=fake_keyboard_input_worker, daemon=True).start()

    # Start game screen capture thread
    threading.Thread(target=capture.start, daemon=True).start()

    route_idx = 0 # Index of route
    t_last_frame = time.time() # Last frame timestamp, for fps calculation
    last_nametag_top_left = (0, 0) # Last position of nametag, for caching
    last_camera = None # Last position of camera, for caching
    switch_status("hunting")

    while IS_RUN:
        if FRAME_BUFFER is None:
            logger.warning(f"Cannot find window:'{GAME_WINDOW_TITLE}'.")
            logger.warning("Please start up maple story!")
            continue

        # Get lastest game screen frame buffer
        with LOCK_FRAME_BUFFER:
            img_window = FRAME_BUFFER[..., :3].copy() # (B,G,R,A) -> (B,G,R)

        # Resize game screen to 1296x759
        img_window = cv2.resize(img_window, (1296, 759), interpolation=cv2.INTER_NEAREST)
        # print(f"img_window.shape = {img_window.shape}") # (759, 1296, 3)

        img_window_gray = cv2.cvtColor(img_window, cv2.COLOR_BGR2GRAY)
        
        # Copy window image for debugging use
        img_debug = img_window.copy()

        ##############################
        ### Rune Warning Detection ###
        ##############################
        # Check whether "PLease remove runes" appear on screen
        x0, y0 = CFG.PLEASE_REMOVE_RUNES_TOP_LEFT
        x1, y1 = CFG.PLEASE_REMOVE_RUNES_BOTTOM_RIGHT
        img_roi = img_window_gray[y0:y1, x0:x1]
        _, score = find_pattern(img_roi, img_please_remove_runes)
        if STATUS == "hunting" and score > CFG.PLEASE_REMOVE_RUNES_SIM_THRES:
            logger.info(f"[Rune Warning] Detect rune warning on screen with score({score})")
            switch_status("finding_rune")

        ########################
        ### Player Detection ###
        ########################
        # Find player location by searching player's name tag
        name_tag_top_left, score = find_pattern(
                                    img_window_gray,
                                    img_nametag,
                                    last_result=last_nametag_top_left
                                )
        if score > CFG.NAMETAG_SIM_THRES:
            # Update last detection
            last_nametag_top_left = name_tag_top_left
        else:
            # Cannot find name tag, than use last detection result
            name_tag_top_left = last_nametag_top_left
        loc_player = (name_tag_top_left[0] - CFG.NAMETAG_OFFSET_X,
                      name_tag_top_left[1] - CFG.NAMETAG_OFFSET_Y)

        ###########################
        ### Player Localization ###
        ###########################
        # Localizae camera position and localize player on map
        camera_top_left, score = find_pattern(
                                    img_map,
                                    img_window_gray[CFG.CAMERA_CEILING:CFG.CAMERA_FLOOR, :],
                                    last_result=last_camera
        )
        last_camera = camera_top_left
        # TODO: make this resizable to improve performance
        map_loc_player = (camera_top_left[0] + loc_player[0],
                          camera_top_left[1] + loc_player[1] - CFG.CAMERA_CEILING,)

        #######################
        ### Runes Detection ###
        #######################
        # Calculate rune detection box around player
        h, w = img_window.shape[:2]
        # Calculate bounding box
        x0 = max(0, loc_player[0] - CFG.RUNE_DETECT_BOX_WIDTH // 2)
        y0 = max(0, loc_player[1] - CFG.RUNE_DETECT_BOX_HEIGHT // 2)
        x1 = min(w, loc_player[0] + CFG.RUNE_DETECT_BOX_WIDTH // 2)
        y1 = min(h, loc_player[1] + CFG.RUNE_DETECT_BOX_HEIGHT // 2)
        # Check runes
        if (x1 - x0) < img_rune.shape[1] or (y1 - y0) < img_rune.shape[0] or \
            STATUS == "near_rune":
            # ROI too small for matching, skipping
            pass
        else:
            _, score = find_pattern(img_window[y0:y1, x0:x1], img_rune)
            if score > CFG.RUNE_DETECT_SIM_THRES:
                logger.info(f"[Rune Detect]Found rune near player with score({score})")
                switch_status("near_rune")
        draw_rectangle(
            img_debug, (x0, y0), (y1-y0, x1-x0),
            (255, 0, 0), "Rune Detection Range"
        )

        #######################
        ### Arrow Detection ###
        #######################
        if STATUS == "finding_rune" or STATUS == "near_rune":
            # Crop arrow detection box
            x, y = CFG.ARROW_BOX_START_POINT
            size = CFG.ARROW_BOX_SIZE
            img_roi = img_window[y:y+size, x:x+size]

            # Loop through all arrow
            for direction, arrow_list in img_arrows.items():
                for img_arrow in arrow_list:
                    _, score = find_pattern_sqdiff(img_roi, img_arrow)
                    if score < CFG.ARROW_BOX_DIF_THRES:
                        logger.info(f"Arrow screen detected with score({score})")
                        screenshot(img_window) # for debugging
                        FAKE_KEYBOARD_COMMAND = "stop"
                        time.sleep(1) # Wait for character to stop
                        switch_status("solving_rune")

            # draw_rectangle(
            #     img_debug, (x, y), (size, size),
            #     (255, 0, 0), "arrow detection box"
            # )

        #########################
        ### Monster Detection ###
        #########################
        # Search monster nearby magic claw range
        dx = CFG.MAGIC_CLAW_RANGE_X + CFG.MONSTER_SEARCH_MARGIN
        dy = CFG.MAGIC_CLAW_RANGE_Y + CFG.MONSTER_SEARCH_MARGIN
        x0 = max(0, loc_player[0] - dx)
        x1 = min(img_window.shape[1], loc_player[0] + dx)
        y0 = max(0, loc_player[1] - dy)
        y1 = min(img_window.shape[0], loc_player[1] + dy)

        # Crop the region of interest (ROI)
        img_roi = img_window[y0:y1, x0:x1]

        monster_info = []
        for monster_name, monster_imgs in monster_dict.items():
            for img_monster in monster_imgs:
                img_monster_gray = cv2.cvtColor(img_monster, cv2.COLOR_BGR2GRAY)
                _, mask_pattern = cv2.threshold(img_monster_gray, 254, 255, cv2.THRESH_BINARY_INV)
                # DEBUG: Show mask
                # cv2.imshow(f"Mask for {monster_name}", mask_pattern)
                # cv2.waitKey(0)  # wait for key press before continuing (for debug)
                res = cv2.matchTemplate(img_roi, img_monster, cv2.TM_SQDIFF_NORMED, mask=mask_pattern)
                match_locations = np.where(res <= CFG.MONSTER_DIF_THRES)
                h, w = img_monster.shape[:2]
                for pt in zip(*match_locations[::-1]):
                    monster_info.append({
                        "name": monster_name,
                        "position": (pt[0] + x0, pt[1] + y0),
                        "size": (h, w),
                        "score": res[pt[1], pt[0]],
                    })

            if IS_DEBUG_MONSTER:
                # Normalize and convert to BGR
                res_norm = cv2.normalize(res, None, 0, 255, cv2.NORM_MINMAX)
                res_norm = np.uint8(res_norm)
                res_bgr = cv2.cvtColor(res_norm, cv2.COLOR_GRAY2BGR)
                # Get image sizes
                h_debug, w_debug = img_window.shape[:2]
                h_res, w_res = res_bgr.shape[:2]
                # Calculate padding needed
                pad_left = (w_debug - w_res) // 2
                pad_right = w_debug - w_res - pad_left
                # Apply horizontal padding to match img_debug width
                res_monster = cv2.copyMakeBorder(
                    res_bgr,
                    top=0, bottom=0,
                    left=pad_left, right=pad_right,
                    borderType=cv2.BORDER_CONSTANT,
                    value=(0, 0, 0)  # black padding
                )

        # Apply Non-Maximum Suppression to monster detection
        monster_info = nms(monster_info, iou_threshold=0.4)

        # Draw attack detection range
        draw_rectangle(
            img_debug, (x0, y0), (dy*2, dx*2),
            (255, 0, 0), "Detection Range"
        )

        # Draw monsters bounding box
        for monster in monster_info:
            draw_rectangle(
                img_debug, monster["position"], monster["size"],
                (0, 255, 0), monster["name"]
            )

        ###############################
        ### Magic Claw Attack Range ###
        ###############################
        # Get magic claw left box
        attack_left_top_left = (loc_player[0] - CFG.MAGIC_CLAW_RANGE_X,
                                loc_player[1] - int(CFG.MAGIC_CLAW_RANGE_Y/2))
        # Get magic claw right box
        attack_right_top_left = (loc_player[0] ,
                                 loc_player[1] - int(CFG.MAGIC_CLAW_RANGE_Y/2))

        #############################
        ### Fake Keyboard Command ###
        #############################
        # Get fake keyboard command from color code route map
        color_code = find_nearest_color_code(
                        map_loc_player,
                        img_routes[route_idx]
        )
        if color_code:
            FAKE_KEYBOARD_COMMAND = color_code["action"]
            # Check if reach goal
            if color_code["action"] == "goal":
                route_idx = (route_idx+1)%len(img_routes)
        else:
            FAKE_KEYBOARD_COMMAND = ""

        # Special logic for each status
        if STATUS == "hunting":
            if is_monster_in_range(monster_info, attack_left_top_left):
                # Attack monster on the left
                FAKE_KEYBOARD_COMMAND = "attack left"
            elif is_monster_in_range(monster_info, attack_right_top_left):
                # Attack monster on the right
                FAKE_KEYBOARD_COMMAND = "attack right"
            elif is_stuck(map_loc_player):
                # Perform a random action
                FAKE_KEYBOARD_COMMAND = random.choice(list(COLOR_CODE.values()))
        elif STATUS == "finding_rune":
            # Check if finding rune timeout
            if time.time() - T_LAST_SWITCH_STATUS > CFG.RUNE_FINDING_TIMEOUT:
                switch_status("resting")
        elif STATUS == "near_rune":
            # Stay in near_rune status for only a few seconds
            if time.time() - T_LAST_SWITCH_STATUS > CFG.NEAR_RUNE_DURATION:
                switch_status("hunting")
        elif STATUS == "solving_rune":
            solve_rune(img_window, img_arrows)
            switch_status("hunting")
        elif STATUS == "resting":
            # Set up resting route
            img_routes = [cv2.cvtColor(cv2.imread(f"maps/{args.map}/route_rest.png"), cv2.COLOR_BGR2RGB)]
            route_idx = 0
        else:
            logger.error(f"Unknown status: {STATUS}")

        ####################
        ### Window Debug ###
        ####################
        # Draw name tag detection box
        draw_rectangle(
            img_debug, name_tag_top_left, img_nametag.shape,
            (0, 255, 0), "Name Tag"
        )

        # Draw player center
        cv2.circle(img_debug, loc_player, radius=3, color=(0, 0, 255), thickness=-1)

        # Draw magic claw right bounding box
        draw_rectangle(
            img_debug, attack_right_top_left,
            (CFG.MAGIC_CLAW_RANGE_Y, CFG.MAGIC_CLAW_RANGE_X),
            (0, 0, 255), "Attack Right Range"
        )

        # Draw magic claw left bounding box
        draw_rectangle(
            img_debug, attack_left_top_left,
            (CFG.MAGIC_CLAW_RANGE_Y, CFG.MAGIC_CLAW_RANGE_X),
            (0, 0, 255), "Attack Left Range"
        )

        # Draw FPS text on top left corner
        fps = round(1.0 / (time.time() - t_last_frame))
        t_last_frame = time.time()
        cv2.putText(
            img_debug, f"FPS: {fps}, Press 's' to save screenshot",
            (10, 30), cv2.FONT_HERSHEY_SIMPLEX, 1.0, (0, 0, 255), 
            2, cv2.LINE_AA
        )

        # Vertically concatenate
        if IS_DEBUG_MONSTER:
            img_debug = cv2.vconcat([img_debug, res_monster])

        # Show debug image
        cv2.imshow("Screen Debug", img_debug)

        #################
        ### Map Debug ###
        #################
        img_map_debug = cv2.cvtColor(img_routes[route_idx].copy(), cv2.COLOR_BGR2RGB)

        # Draw camera
        cv2.circle(img_map_debug, camera_top_left, radius=3, color=(0, 0, 255), thickness=-1)
        cv2.putText(img_map_debug, "Camera", (camera_top_left[0], camera_top_left[1] - 10),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.5, (0, 255, 0), 1)

        # Draw player center
        cv2.circle(img_map_debug, map_loc_player, radius=3, color=(0, 0, 255), thickness=-1)
        cv2.putText(img_map_debug, "Player", (map_loc_player[0], map_loc_player[1] - 10),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.5, (0, 255, 0), 1)

        # Draw search range
        draw_rectangle(
            img_map_debug,
            (map_loc_player[0] - CFG.COLOR_CODE_SEARCH_RANGE,
             map_loc_player[1] - CFG.COLOR_CODE_SEARCH_RANGE),
            (CFG.COLOR_CODE_SEARCH_RANGE*2, CFG.COLOR_CODE_SEARCH_RANGE*2),
            (0, 0, 255), "Color Code Search Range"
        )

        # Draw a straigt line from map_loc_player to color_code["pixel"]
        if color_code is not None:
            cv2.line(
                img_map_debug,
                map_loc_player,      # start point
                color_code["pixel"], # end point
                (0, 255, 0),         # green line
                1                    # thickness
            )

        # Show debug image, donw size
        h, w = img_map_debug.shape[:2]
        img_map_debug = cv2.resize(img_map_debug, (w // 2, h // 2),
                                   interpolation=cv2.INTER_NEAREST)
        # img_map_debug = cv2.resize(img_map_debug)
        cv2.imshow("Map Debug", img_map_debug)

        # Exit if 'q' is pressed
        key = cv2.waitKey(1) & 0xFF  # Get the pressed key (8-bit)
        if key == ord('s'):
            screenshot(img_window)
        elif key == ord('q'):
            break

    cv2.destroyAllWindows()

if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument(
        '--disable_control',
        action='store_true',
        help='Disable fake keyboard input'
    )

    # Argument to specify map name
    parser.add_argument(
        '--map',
        type=str,
        default='ant_cave_2',
        help='Specify the map name'
    )

    parser.add_argument(
        "--monsters",
        type=str,
        default="all",
        help="Specify which monsters to load, comma-separated (e.g., --monsters green_mushroom,zombie_mushroom)"
    )

    args = parser.parse_args()

    main(args)
