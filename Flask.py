from time import sleep
from flask import Flask, Response, redirect, url_for, request, session, abort, render_template
import cv2
import depthai as dai
import blobconverter
from MultiMsgSync import TwoStageHostSeqSync
from tools import *

app = Flask(__name__)

def gen_frames():
    pipeline = dai.Pipeline()
    cam_rgb = pipeline.createColorCamera()
    cam_rgb.setPreviewSize(640, 480)
    cam_rgb.setBoardSocket(dai.CameraBoardSocket.RGB)
    cam_rgb.setResolution(dai.ColorCameraProperties.SensorResolution.THE_1080_P)
    cam_rgb.setInterleaved(False)
    cam_rgb.setColorOrder(dai.ColorCameraProperties.ColorOrder.RGB)

    xout_rgb = pipeline.createXLinkOut()
    xout_rgb.setStreamName("rgb")
    cam_rgb.preview.link(xout_rgb.input)

    with dai.Device(pipeline) as device:
        device.startPipeline()

        q_rgb = device.getOutputQueue(name="rgb", maxSize=4, blocking=False)

        while True:
            in_rgb = q_rgb.get() 
            frame = in_rgb.getCvFrame()
            success, buffer = cv2.imencode('.jpg', frame)
            frame = buffer.tobytes()
            yield (b'--frame\r\n'
                    b'Content-Type: image/jpeg\r\n\r\n' + frame + b'\r\n')

def dect_head():
    def create_pipeline(stereo):
        pipeline = dai.Pipeline()

        print("Creating Color Camera...")
        cam = pipeline.create(dai.node.ColorCamera)
        cam.setPreviewSize(400, 400)
        cam.setResolution(dai.ColorCameraProperties.SensorResolution.THE_1080_P)
        cam.setInterleaved(False)
        cam.setBoardSocket(dai.CameraBoardSocket.RGB)

        cam_xout = pipeline.create(dai.node.XLinkOut)
        cam_xout.setStreamName("color")
        cam.preview.link(cam_xout.input)

        # Workaround: remove in 2.18, use `cam.setPreviewNumFramesPool(10)`
        # This manip uses 15*3.5 MB => 52 MB of RAM.
        copy_manip = pipeline.create(dai.node.ImageManip)
        copy_manip.setNumFramesPool(15)
        copy_manip.setMaxOutputFrameSize(3499200)
        cam.preview.link(copy_manip.inputImage)

        # ImageManip will resize the frame before sending it to the Face detection NN node
        face_det_manip = pipeline.create(dai.node.ImageManip)
        face_det_manip.initialConfig.setResize(300, 300)
        face_det_manip.initialConfig.setFrameType(dai.RawImgFrame.Type.RGB888p)
        copy_manip.out.link(face_det_manip.inputImage)

        if stereo:
            monoLeft = pipeline.create(dai.node.MonoCamera)
            monoLeft.setResolution(dai.MonoCameraProperties.SensorResolution.THE_400_P)
            monoLeft.setBoardSocket(dai.CameraBoardSocket.LEFT)

            monoRight = pipeline.create(dai.node.MonoCamera)
            monoRight.setResolution(dai.MonoCameraProperties.SensorResolution.THE_400_P)
            monoRight.setBoardSocket(dai.CameraBoardSocket.RIGHT)

            stereo = pipeline.create(dai.node.StereoDepth)
            stereo.setDefaultProfilePreset(dai.node.StereoDepth.PresetMode.HIGH_DENSITY)
            stereo.setDepthAlign(dai.CameraBoardSocket.RGB)
            monoLeft.out.link(stereo.left)
            monoRight.out.link(stereo.right)

            # Spatial Detection network if OAK-D
            print("OAK-D detected, app will display spatial coordiantes")
            face_det_nn = pipeline.create(dai.node.MobileNetSpatialDetectionNetwork)
            face_det_nn.setBoundingBoxScaleFactor(0.8)
            face_det_nn.setDepthLowerThreshold(100)
            face_det_nn.setDepthUpperThreshold(5000)
            stereo.depth.link(face_det_nn.inputDepth)
        else: # Detection network if OAK-1
            print("OAK-1 detected, app won't display spatial coordiantes")
            face_det_nn = pipeline.create(dai.node.MobileNetDetectionNetwork)

        face_det_nn.setConfidenceThreshold(0.7)
        face_det_nn.setBlobPath(blobconverter.from_zoo(name="face-detection-retail-0004", shaves=6))
        face_det_manip.out.link(face_det_nn.input)

        # Send face detections to the host (for bounding boxes)
        face_det_xout = pipeline.create(dai.node.XLinkOut)
        face_det_xout.setStreamName("detection")
        face_det_nn.out.link(face_det_xout.input)

        # Script node will take the output from the face detection NN as an input and set ImageManipConfig
        # to the 'recognition_manip' to crop the initial frame
        image_manip_script = pipeline.create(dai.node.Script)
        face_det_nn.out.link(image_manip_script.inputs['face_det_in'])

        # Only send metadata, we are only interested in timestamp, so we can sync
        # depth frames with NN output
        face_det_nn.passthrough.link(image_manip_script.inputs['passthrough'])
        copy_manip.out.link(image_manip_script.inputs['preview'])

        image_manip_script.setScript("""
        import time
        msgs = dict()

        def add_msg(msg, name, seq = None):
            global msgs
            if seq is None:
                seq = msg.getSequenceNum()
            seq = str(seq)
            # node.warn(f"New msg {name}, seq {seq}")

            # Each seq number has it's own dict of msgs
            if seq not in msgs:
                msgs[seq] = dict()
            msgs[seq][name] = msg

            # To avoid freezing (not necessary for this ObjDet model)
            if 15 < len(msgs):
                node.warn(f"Removing first element! len {len(msgs)}")
                msgs.popitem() # Remove first element

        def get_msgs():
            global msgs
            seq_remove = [] # Arr of sequence numbers to get deleted
            for seq, syncMsgs in msgs.items():
                seq_remove.append(seq) # Will get removed from dict if we find synced msgs pair
                # node.warn(f"Checking sync {seq}")

                # Check if we have both detections and color frame with this sequence number
                if len(syncMsgs) == 2: # 1 frame, 1 detection
                    for rm in seq_remove:
                        del msgs[rm]
                    # node.warn(f"synced {seq}. Removed older sync values. len {len(msgs)}")
                    return syncMsgs # Returned synced msgs
            return None

        def correct_bb(xmin,ymin,xmax,ymax):
            if xmin < 0: xmin = 0.001
            if ymin < 0: ymin = 0.001
            if xmax > 1: xmax = 0.999
            if ymax > 1: ymax = 0.999
            return [xmin,ymin,xmax,ymax]

        while True:
            time.sleep(0.001) # Avoid lazy looping

            preview = node.io['preview'].tryGet()
            if preview is not None:
                add_msg(preview, 'preview')

            face_dets = node.io['face_det_in'].tryGet()
            if face_dets is not None:
                # TODO: in 2.18.0.0 use face_dets.getSequenceNum()
                passthrough = node.io['passthrough'].get()
                seq = passthrough.getSequenceNum()
                add_msg(face_dets, 'dets', seq)

            sync_msgs = get_msgs()
            if sync_msgs is not None:
                img = sync_msgs['preview']
                dets = sync_msgs['dets']
                for i, det in enumerate(dets.detections):
                    cfg = ImageManipConfig()
                    bb = correct_bb(det.xmin-0.03, det.ymin-0.03, det.xmax+0.03, det.ymax+0.03)
                    cfg.setCropRect(*bb)
                    # node.warn(f"Sending {i + 1}. det. Seq {seq}. Det {det.xmin}, {det.ymin}, {det.xmax}, {det.ymax}")
                    cfg.setResize(60, 60)
                    cfg.setKeepAspectRatio(False)
                    node.io['manip_cfg'].send(cfg)
                    node.io['manip_img'].send(img)
        """)
        recognition_manip = pipeline.create(dai.node.ImageManip)
        recognition_manip.initialConfig.setResize(60, 60)
        recognition_manip.setWaitForConfigInput(True)
        image_manip_script.outputs['manip_cfg'].link(recognition_manip.inputConfig)
        image_manip_script.outputs['manip_img'].link(recognition_manip.inputImage)

        # Second stange recognition NN
        print("Creating recognition Neural Network...")
        recognition_nn = pipeline.create(dai.node.NeuralNetwork)
        recognition_nn.setBlobPath(blobconverter.from_zoo(name="head-pose-estimation-adas-0001", shaves=6))
        recognition_manip.out.link(recognition_nn.input)

        recognition_xout = pipeline.create(dai.node.XLinkOut)
        recognition_xout.setStreamName("recognition")
        recognition_nn.out.link(recognition_xout.input)

        return pipeline

    with dai.Device() as device:
        stereo = 1 < len(device.getConnectedCameras())
        device.startPipeline(create_pipeline(stereo))

        sync = TwoStageHostSeqSync()
        queues = {}
        # Create output queues
        for name in ["color", "detection", "recognition"]:
            queues[name] = device.getOutputQueue(name)

        while True:
            for name, q in queues.items():
                # Add all msgs (color frames, object detections and recognitions) to the Sync class.
                if q.has():
                    sync.add_msg(q.get(), name)

            msgs = sync.get_msgs()
            if msgs is not None:
                frame = msgs["color"].getCvFrame()
                detections = msgs["detection"].detections
                recognitions = msgs["recognition"]

                for i, detection in enumerate(detections):
                    bbox = frame_norm(frame, (detection.xmin, detection.ymin, detection.xmax, detection.ymax))

                    # Decoding of recognition results
                    rec = recognitions[i]
                    yaw = rec.getLayerFp16('angle_y_fc')[0]
                    pitch = rec.getLayerFp16('angle_p_fc')[0]
                    roll = rec.getLayerFp16('angle_r_fc')[0]
                    decode_pose(yaw, pitch, roll, bbox, frame)

                    cv2.rectangle(frame, (bbox[0], bbox[1]), (bbox[2], bbox[3]), (10, 245, 10), 2)
                    y = (bbox[1] + bbox[3]) // 2
                    if stereo:
                        # You could also get detection.spatialCoordinates.x and detection.spatialCoordinates.y coordinates
                        coords = "Z: {:.2f} m".format(detection.spatialCoordinates.z/1000)
                        cv2.putText(frame, coords, (bbox[0], y + 60), cv2.FONT_HERSHEY_TRIPLEX, 1, (0, 0, 0), 8)
                        cv2.putText(frame, coords, (bbox[0], y + 60), cv2.FONT_HERSHEY_TRIPLEX, 1, (255, 255, 255), 2)


                success, buffer = cv2.imencode('.jpg', frame)
                frame = buffer.tobytes()
                yield (b'--frame\r\n'
                    b'Content-Type: image/jpeg\r\n\r\n' + frame + b'\r\n')
                
    
@app.route('/video_feed')
def video_feed():
    return Response(gen_frames(),
                    mimetype='multipart/x-mixed-replace; boundary=frame')

@app.route('/dect_video')
def dect_video():
    sleep(10)
    return Response(dect_head(),
                    mimetype='multipart/x-mixed-replace; boundary=frame')
    
@app.route('/SomeFunction')    
def SomeFunction():
    print('In SomeFunction')
    return "Nothing"
   
@app.route('/')
def index():
    return render_template('teleop.html')

if __name__ == "__main__":
    app.run(debug=True,host="0.0.0.0",threaded=True)