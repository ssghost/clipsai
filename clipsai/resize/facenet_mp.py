"""
Resizing/cropping a media file to a different aspect ratio

Notes
-----
- ROI is "region of interest"
"""
# standard library imports
import logging

# current package imports
from .exceptions import FaceNetMediaPipeResizerError
from .rect import Rect

# local package imports
from media.editor import MediaEditor
from media.video_file import VideoFile
from ml.utils.image import extract_frames, calc_img_bytes
from ml.utils import pytorch
from sklearn.cluster import KMeans
from utils.conversions import bytes_to_gibibytes

# 3rd party imports
import cv2
from facenet_pytorch import MTCNN
import mediapipe as mp
import numpy as np
import torch


class FaceNetMediaPipeResizer:
    """
    A class for calculating the initial coordinates for resizing by using
    segmentation and face detection.
    """
    FACE_DETECT_WIDTH = 960
    SAMPLES_PER_SEGMENT = 13

    def __init__(self, device: str = None) -> None:
        """
        Initialize the DLibResizer class
        """
        if device is None:
            device = pytorch.get_compute_device()
        pytorch.assert_compute_device_available(device)
        logging.info("FaceNet using device: {}".format(device))

        self._face_detector = MTCNN(margin=20, post_process=False, device=device)
        # media pipe automatically uses gpu if available
        self._face_mesher = mp.solutions.face_mesh.FaceMesh()
        self._media_editor = MediaEditor()

    def resize(
        self,
        video_file: VideoFile,
        speaker_segments: list[dict],
        scene_changes: list[float],
        aspect_ratio: tuple = (9, 16),
    ) -> list[dict]:
        """
        Calculates the coordinates to resize the video to for different segments given
        the diarized speaker segments and the desired aspect ratio.

        Parameters
        ----------
        video_file: VideoFile
            the video file to resize
        speaker_segments: list[dict]
            List of speaker segments (dictionaries), each with the following keys
                speakers: list[int]
                    list of speaker numbers for the speakers talking in the segment
                startTime: float
                    start time of the segment in seconds
                endTime: float
                    end time of the segment in seconds
        scene_changes: list[float]
            List of scene change times in seconds.
        aspect_ratio: tuple[int, int]
            the width:height aspect ratio to resize the video to

        Returns
        -------
        dict[int, int, list[dict]]
            dictionary with the following keys:
                originalWidth: int
                    original width of the video
                originalHeight: int
                    original height of the video
                resizeWidth: int
                    resized width of the video
                resizeHeight: int
                    resized height of the video
                segments: list
                    list of speaker segments (dictionaries) with the following keys
                        speakers: list[int]
                            the speaker labels of the speakers talking in the segment
                        startTime: float
                            the start time of the segment
                        endTime: float
                            the end time of the segment
                        x: int
                            x-coordinate of the top left corner of the resized segment
                        y: int
                            y-coordinate of the top left corner of the resized segment
        """
        logging.info("Video Resolution: {}x{}".format(
            video_file.get_width_pixels(), video_file.get_height_pixels()
        ))
        # calculate resize dimensions
        resize_width, resize_height = (
            self._calc_resize_width_and_height_pixels(
                original_width_pixels=video_file.get_width_pixels(),
                original_height_pixels=video_file.get_height_pixels(),
                resize_aspect_ratio=aspect_ratio,
            )
        )

        logging.info("Merging {} speaker segments with {} scene changes.".format(
            len(speaker_segments), len(scene_changes)
        ))
        segments = self._merge_scene_change_and_speaker_segments(
            speaker_segments=speaker_segments,
            scene_changes=scene_changes,
        )
        logging.info("Video has {} distinct segments.".format(len(segments)))

        logging.info("Determining the first second with a face for each segment.")
        segments = self._find_first_sec_with_face_for_each_segment(
            segments, video_file
        )

        logging.info("Determining the region of interest for {} segments.".format(
            len(segments)
        ))
        segments = self._add_x_y_coords_to_each_segment(
            segments=segments,
            video_file=video_file,
            resize_width=resize_width,
            resize_height=resize_height,
        )

        logging.info("Merging identical segments together.")
        unmerge_segments_length = len(segments)
        segments = self._merge_identical_segments(segments, video_file)
        logging.info("Merged {} identical segments.".format(
            unmerge_segments_length - len(segments)
        ))

        resize_info = {
            "originalWidth": video_file.get_width_pixels(),
            "originalHeight": video_file.get_height_pixels(),
            "resizeWidth": resize_width,
            "resizeHeight": resize_height,
            "segments": segments,
        }

        return resize_info

    def _calc_resize_width_and_height_pixels(
        self,
        original_width_pixels: int,
        original_height_pixels: int,
        resize_aspect_ratio: tuple,
    ) -> tuple[int, int]:
        """
        Calculate the number of pixels along the width and height to resize the video
        to based on the desired aspect ratio.

        Parameters
        ----------
        original_pixels_width: int
            Number of pixels along the width of the original video.
        original_pixels_height: int
            Number of pixels along the height of the original video
        resize_aspect_ratio: tuple[int, int]
            The width:height aspect ratio to resize the video to

        Returns
        -------
        tuple[int, int]
            The number of pixels along the width and height to resize the video to
        """
        resize_ar_width, resize_ar_height = resize_aspect_ratio
        desired_aspect_ratio = resize_ar_width / resize_ar_height
        original_aspect_ratio = original_width_pixels / original_height_pixels

        # original aspect ratio is wider than desired aspect ratio
        if original_aspect_ratio > desired_aspect_ratio:
            resize_height_pixels = original_height_pixels
            resize_width_pixels = int(
                resize_height_pixels * resize_ar_width / resize_ar_height
            )
        # original aspect ratio is taller than desired aspect ratio
        else:
            resize_width_pixels = original_width_pixels
            resize_height_pixels = int(
                resize_width_pixels * resize_ar_height / resize_ar_width
            )

        return resize_width_pixels, resize_height_pixels

    def _merge_scene_change_and_speaker_segments(
        self,
        speaker_segments: list[dict],
        scene_changes: list[float],
    ) -> list[dict]:
        """
        Merge scene change segments with speaker segments.

        Parameters
        ----------
        speaker_segments: list[dict]
            List of speaker_segments (dictionaries), each with the following keys
                speakers: list[int]
                    list of speaker numbers for the speakers talking in the segment
                startTime: float
                    start time of the segment in seconds
                endTime: float
                    end time of the segment in seconds
        scene_changes: list[float]
            List of scene change times in seconds.

        Returns
        -------
        list[dict]
            List of segments (dictionaries), each with the following keys
                speakers: list[int]
                    list of speaker numbers for the speakers talking in the segment
                startTime: float
                    start time of the segment in seconds
                endTime: float
                    end time of the segment in seconds
        """
        segments_idx = 0
        for scene_change_sec in scene_changes:
            segment = speaker_segments[segments_idx]
            while scene_change_sec > (segment["endTime"]):
                segments_idx += 1
                segment = speaker_segments[segments_idx]
            # scene change is close to speaker segment end -> merge the two
            if 0 < (segment["endTime"] - scene_change_sec) < 0.25:
                segment["endTime"] = scene_change_sec
                if segments_idx == len(speaker_segments) - 1:
                    continue
                next_segment = speaker_segments[segments_idx + 1]
                next_segment["startTime"] = scene_change_sec
                continue
            # scene change is close to speaker segment start -> merge the two
            if 0 < (scene_change_sec - segment["startTime"]) < 0.25:
                segment["startTime"] = scene_change_sec
                if segments_idx == 0:
                    continue
                prev_segment = speaker_segments[segments_idx - 1]
                prev_segment["endTime"] = scene_change_sec
                continue
            # scene change already exists
            if scene_change_sec == segment["endTime"]:
                continue
            # add scene change to segments
            new_segment = {
                "startTime": scene_change_sec,
                "speakers": segment["speakers"],
                "endTime": segment["endTime"],
            }
            segment["endTime"] = scene_change_sec
            speaker_segments = (
                speaker_segments[: segments_idx + 1] +
                [new_segment] +
                speaker_segments[segments_idx + 1:]
            )

        return speaker_segments

    def _find_first_sec_with_face_for_each_segment(
        self,
        segments: list[dict],
        video_file: VideoFile,
    ) -> tuple:
        """
        Find the first frame in a segment with a face.

        Parameters
        ----------
        segments: list[dict]
            List of speaker segments (dictionaries), each with the following keys
                speakers: list[int]
                    list of speaker numbers for the speakers talking in the segment
                startTime: float
                    start time of the segment in seconds
                endTime: float
                    end time of the segment in seconds
        video_file: VideoFile
            The video file to analyze.

        Returns
        -------
        list[dict]
            List of speaker segments (dictionaries), each with the following keys
                speakers: list[int]
                    list of speaker numbers for the speakers talking in the segment
                startTime: float
                    start time of the segment in seconds
                endTime: float
                    end time of the segment in seconds
                firstFaceSec: float
                    the first second in the segment with a face
                foundFace: bool
                    whether or not a face was found in the segment
        """
        for segment in segments:
            start_sec = segment["startTime"]
            end_sec = segment["endTime"]
            # start looking for faces an eighth of the way through the segment
            segment["firstFaceSec"] = start_sec + (end_sec - start_sec) / 8
            segment["foundFace"] = False
            segment["isAnalyzed"] = False

        batch_period = 1  # interval length to sample each segment at each iteration
        sample_period = 1  # interval between consecutive samples
        analyzed_segments = 0
        while analyzed_segments < len(segments):
            # select times to detect faces from
            detect_secs = []
            for segment in segments:
                if segment["isAnalyzed"] is True:
                    continue
                segment_secs_left = segment["endTime"] - segment["firstFaceSec"]
                num_samples = min(batch_period, segment_secs_left) // sample_period
                num_samples = max(1, int(num_samples))
                segment["numSamples"] = num_samples
                for i in range(num_samples):
                    detect_secs.append(segment["firstFaceSec"] + i * sample_period)

            # detect faces
            n_batches = self._calc_n_batches(video_file, len(detect_secs))
            frames_per_batch = int(len(detect_secs) // n_batches + 1)
            face_detections = []
            for i in range(n_batches):
                frames = extract_frames(
                    video_file,
                    detect_secs[
                        i * frames_per_batch:
                        min((i + 1) * frames_per_batch, len(detect_secs))
                    ]
                )
                face_detections += self._detect_faces(frames)

            # check if any faces were found for each segment
            idx = 0
            for segment in segments:
                # segment already analyzed
                if segment["isAnalyzed"] is True:
                    continue
                segment_idx = idx
                # check if any faces were found
                for _ in range(segment["numSamples"]):
                    faces = face_detections[idx]
                    if faces is not None:
                        segment["foundFace"] = True
                        break
                    segment["firstFaceSec"] += sample_period
                    idx += 1
                # update segment analyzation status
                is_analyzed = (
                    segment["foundFace"] is True or
                    segment["firstFaceSec"] >= segment["endTime"] - 0.25
                )
                if is_analyzed:
                    segment["isAnalyzed"] = True
                    analyzed_segments += 1
                idx = segment_idx + segment["numSamples"]

            # increase period for next iteration
            batch_period = (batch_period + 3) * 2

        for segment in segments:
            del segment["numSamples"]
            del segment["isAnalyzed"]

        return segments

    def _calc_n_batches(
        self,
        video_file: VideoFile,
        num_frames: int,
    ) -> int:
        """
        Calculate the number of batches to use for extracting frames from a video file
        and detecting the face in each frame.

        Parameters
        ----------
        video_file: VideoFile
            The video file to analyze.
        num_frames: int
            The number of frames to analyze.

        Returns
        -------
        int
            The number of batches to use.
        """
        # calculate memory needed to extract frames to CPU
        vid_height = video_file.get_height_pixels()
        vid_width = video_file.get_width_pixels()
        bytes_per_frame = calc_img_bytes(vid_height, vid_width, 3)
        total_extract_bytes = num_frames * bytes_per_frame
        logging.info("Need {:.3f} GiB to extract (at most) {} frames.".format(
            bytes_to_gibibytes(total_extract_bytes), num_frames
        ))

        # calculate memory needed to detect faces -> could be CPU or GPU
        downsample_factor = max(vid_width / self.FACE_DETECT_WIDTH, 1)
        face_detect_height = int(video_file.get_height_pixels() // downsample_factor)
        logging.info("Face detection dimensions: {}x{}".format(
            face_detect_height, self.FACE_DETECT_WIDTH
        ))
        bytes_per_frame = calc_img_bytes(face_detect_height, self.FACE_DETECT_WIDTH, 3)
        total_face_detect_bytes = num_frames * bytes_per_frame
        logging.info("Need {:.3f} GiB to detect faces from (at most) {} frames.".format(
            bytes_to_gibibytes(total_face_detect_bytes), num_frames
        ))

        # calculate number of batches to add x and y coordinates
        # Tested for a single NVIDIA T4 GPU w/ 16GiB of memory
        free_gpu_memory = 800000000  # ~750 MiB
        # Tested for Google's C3D servers with ~7.5 cores and 27GiB of memory
        free_cpu_memory = 8000000000  # ~7.5 GiB
        if torch.cuda.is_available():
            n_extract_batches = int(total_extract_bytes // free_cpu_memory + 1)
            n_face_detect_batches = total_face_detect_bytes // free_gpu_memory + 1
        else:
            total_extract_bytes += total_face_detect_bytes
            n_extract_batches = total_extract_bytes // free_cpu_memory + 1
            n_face_detect_batches = 0

        n_batches = int(max(n_extract_batches, n_face_detect_batches))
        cpu_mem_per_batch = bytes_to_gibibytes(total_extract_bytes // n_batches)
        if n_face_detect_batches == 0:
            gpu_mem_per_batch = 0
        else:
            gpu_mem_per_batch = bytes_to_gibibytes(
                total_face_detect_bytes // n_batches
            )
        logging.info(
            "Using {} batches to extract and detect frames. Need {:.3f} GiB of CPU "
            "memory per batch and {:.3f} GiB of GPU memory per batch".format(
                n_batches, cpu_mem_per_batch, gpu_mem_per_batch,
            )
        )
        return n_batches

    def _detect_faces(
        self,
        frames: list[np.ndarray],
    ) -> list[np.ndarray]:
        """
        Detect faces in a list of frames.

        Parameters
        ----------
        video_file: VideoFile
            The video file to detect faces in.
        detect_secs: list[float]
            The seconds to detect faces in.

        Returns
        -------
        list[np.ndarray]
            The face detections for each frame.
        """
        if len(frames) == 0:
            logging.info("No frames to detect faces in.")
            return []

        # resize the frames
        logging.info("Detecting faces in {} frames.".format(len(frames)))
        downsample_factor = max(frames[0].shape[1] / self.FACE_DETECT_WIDTH, 1)
        detect_height = int(frames[0].shape[0] / downsample_factor)
        resized_frames = []
        for frame in frames:
            resized_frame = cv2.resize(frame, (self.FACE_DETECT_WIDTH, detect_height))
            if torch.cuda.is_available():
                resized_frame = torch.from_numpy(resized_frame).to(
                    device="cuda", dtype=torch.uint8
                )
            resized_frames.append(resized_frame)

        # detect faces in batches
        if torch.cuda.is_available():
            resized_frames = torch.stack(resized_frames)
        detections, _ = self._face_detector.detect(resized_frames)

        # detections are returned as numpy arrays regardless
        face_detections = []
        for detection in detections:
            if detection is not None:
                detection[detection < 0] = 0
                detection = (detection * downsample_factor).astype(np.int16)
            face_detections.append(detection)

        logging.info("Detected faces in {} frames.".format(len(face_detections)))
        return face_detections

    def _add_x_y_coords_to_each_segment(
        self,
        segments: list[dict],
        video_file: VideoFile,
        resize_width: int,
        resize_height: int,
    ) -> list[dict]:
        """
        Add the x and y coordinates to resize each segment to.

        Parameters
        ----------
        segments: list[dict]
            List of speaker segments (dictionaries), each with the following keys
                speakers: list[int]
                    list of speaker numbers for the speakers talking in the segment
                startTime: float
                    start time of the segment in seconds
                endTime: float
                    end time of the segment in seconds
                firstFaceSec: float
                    the first second in the segment with a face
                foundFace: bool
                    whether or not a face was found in the segment
        video_file: VideoFile
            The video file to analyze.
        resize_width: int
            The width to resize the video to.
        resize_height: int
            The height to resize the video to.

        Returns
        -------
        list[dict]
            List of speaker segments (dictionaries), each with the following keys
                speakers: list[int]
                    list of speaker numbers for the speakers talking in the segment
                startTime: float
                    start time of the segment in seconds
                endTime: float
                    end time of the segment in seconds
                x: int
                    x-coordinate of the top left corner of the resized segment
                y: int
                    y-coordinate of the top left corner of the resized segment
        """
        num_segments = len(segments)
        num_frames = num_segments * self.SAMPLES_PER_SEGMENT
        n_batches = self._calc_n_batches(video_file, num_frames)
        segments_per_batch = int(num_segments // n_batches + 1)
        segments_with_xy_coords = []
        for i in range(n_batches):
            logging.info("Analyzing batch {} of {}.".format(i, n_batches))
            cur_segments = segments[
                i * segments_per_batch:
                min((i + 1) * segments_per_batch, len(segments))
            ]
            if len(cur_segments) == 0:
                logging.info("No segments left to analyze. (Batch {})".format(i))
                break
            segments_with_xy_coords += self._add_x_y_coords_to_each_segment_batch(
                segments=cur_segments,
                video_file=video_file,
                resize_width=resize_width,
                resize_height=resize_height,
            )
        return segments_with_xy_coords

    def _add_x_y_coords_to_each_segment_batch(
        self,
        segments: list[dict],
        video_file: VideoFile,
        resize_width: int,
        resize_height: int,
    ) -> list[dict]:
        """
        Add the x and y coordinates to resize each segment to for a given batch.

        Parameters
        ----------
        segments: list[dict]
            List of speaker segments (dictionaries), each with the following keys
                speakers: list[int]
                    list of speaker numbers for the speakers talking in the segment
                startTime: float
                    start time of the segment in seconds
                endTime: float
                    end time of the segment in seconds
                firstFaceSec: float
                    the first second in the segment with a face
                foundFace: bool
                    whether or not a face was found in the segment
        video_file: VideoFile
            The video file to analyze.
        resize_width: int
            The width to resize the video to.
        resize_height: int
            The height to resize the video to.

        Returns
        -------
        list[dict]
            List of speaker segments (dictionaries), each with the following keys
                speakers: list[int]
                    list of speaker numbers for the speakers talking in the segment
                startTime: float
                    start time of the segment in seconds
                endTime: float
                    end time of the segment in seconds
                x: int
                    x-coordinate of the top left corner of the resized segment
                y: int
                    y-coordinate of the top left corner of the resized segment
        """
        fps = video_file.get_frame_rate()

        # define frames to analyze from each segment
        detect_secs = []
        for segment in segments:
            if segment["foundFace"] is False:
                continue
            # define interval over which to analyze faces
            end_sec = segment["endTime"]
            first_face_sec = segment["firstFaceSec"]
            analyze_end_sec = end_sec - (end_sec - first_face_sec) / 8
            # get sample locations
            frames_left = int((analyze_end_sec - first_face_sec) * fps + 1)
            num_samples = min(frames_left, self.SAMPLES_PER_SEGMENT)
            segment["numSamples"] = num_samples
            # add first face, sample the rest
            detect_secs.append(first_face_sec)
            sample_frames = np.sort(
                np.random.choice(range(1, frames_left), num_samples - 1, replace=False)
            )
            for sample_frame in sample_frames:
                detect_secs.append(first_face_sec + sample_frame / fps)

        # detect faces from each segment
        logging.info("Extracting {} frames".format(len(detect_secs)))
        frames = extract_frames(video_file, detect_secs)
        logging.info("Extracted {} frames".format(len(detect_secs)))
        face_detections = self._detect_faces(frames)

        logging.info("Calculating ROI for {} segments.".format(len(segments)))
        # find roi for each segment
        idx = 0
        for segment in segments:
            # find segment roi
            if segment["foundFace"] is True:
                roi = self._calc_segment_roi(
                    frames=frames[idx: idx + segment["numSamples"]],
                    face_detections=face_detections[idx: idx + segment["numSamples"]]
                )
                idx += segment["numSamples"]
                del segment["numSamples"]
            else:
                logging.debug("Using default ROI for segment {}".format(segment))
                roi = Rect(
                    x=(video_file.get_width_pixels()) // 4,
                    y=(video_file.get_height_pixels()) // 4,
                    width=(video_file.get_width_pixels()) // 2,
                    height=(video_file.get_height_pixels()) // 2,
                )
            del segment["foundFace"]
            del segment["firstFaceSec"]

            # add crop coordinates to segment
            crop = self._calc_crop(roi, resize_width, resize_height)
            segment["x"] = int(crop.x)
            segment["y"] = int(crop.y)
        logging.info("Calculated ROI for {} segments.".format(len(segments)))

        return segments

    def _calc_segment_roi(
        self,
        frames: list[np.ndarray],
        face_detections: list[np.ndarray],
    ) -> Rect:
        """
        Find the region of interest (ROI) for a given segment.

        Parameters
        ----------
        frames: np.ndarray
            The frames to analyze.
        face_detections: np.ndarray
            The face detection outputs for each frame

        Returns
        -------
        Rect
            The region of interest (ROI) for the segment.
        """
        segment_roi = None

        # preprocessing for kmeans
        bounding_boxes: list[np.ndarray] = []
        k = 0
        for face_detection in face_detections:
            if face_detection is None:
                continue
            k = max(k, len(face_detection))
            for bounding_box in face_detection:
                bounding_boxes.append(bounding_box)

        # no faces detected
        if k == 0:
            raise FaceNetMediaPipeResizerError("No faces detected in segment.")
        bounding_boxes = np.stack(bounding_boxes)

        # single face detected
        if k == 1:
            box = np.mean(bounding_boxes, axis=0).astype(np.int16)
            x1, y1, x2, y2 = box
            segment_roi = Rect(x1, y1, x2 - x1, y2 - y1)
            return segment_roi

        # use kmeans to group the same bounding boxes together
        kmeans = KMeans(
            n_clusters=k,
            init="k-means++",
            n_init=2,
            random_state=0
        ).fit(bounding_boxes)
        bounding_box_labels = kmeans.labels_
        bounding_box_groups: list[list[dict]] = [[] for _ in range(k)]
        kmeans_idx = 0
        for i, face_detection in enumerate(face_detections):
            if face_detection is None:
                continue
            for bounding_box in face_detection:
                assert np.sum(bounding_box < 0) == 0
                bounding_box_label = bounding_box_labels[kmeans_idx]
                bounding_box_groups[bounding_box_label].append(
                    {"bounding_box": bounding_box, "frame": i}
                )
                kmeans_idx += 1

        # find the face who's mouth moves the most
        max_mouth_movement = 0
        for bounding_box_group in bounding_box_groups:
            mouth_movement, roi = self._calc_mouth_movement(bounding_box_group, frames)
            if mouth_movement > max_mouth_movement:
                max_mouth_movement = mouth_movement
                segment_roi = roi

        # no mouth movement detected -> choose face with the most frames
        if segment_roi is None:
            logging.debug("No mouth movement detected for segment.")
            max_frames = 0
            for bounding_box_group in bounding_box_groups:
                if len(bounding_box_group) > max_frames:
                    max_frames = len(bounding_box_group)
                    avg_box = np.array([0, 0, 0, 0])
                    for bounding_box_data in bounding_box_group:
                        avg_box += bounding_box_data["bounding_box"]
                    avg_box = avg_box / len(bounding_box_group)
                    avg_box = avg_box.astype(np.int16)
                    segment_roi = Rect(
                        avg_box[0],
                        avg_box[1],
                        avg_box[2] - avg_box[0],
                        avg_box[3] - avg_box[1]
                    )

        return segment_roi

    def _calc_mouth_movement(
        self,
        bounding_box_group: list[dict[np.ndarray, int]],
        frames: list[np.ndarray],
    ) -> tuple[float, Rect]:
        """
        Calculates the mouth movement for a group of faces. These faces are assumed to
        all be the same person in different frames of the source video. Further, the
        frames are assumed to be in order of occurrence (earlier frames first).

        Parameters
        ----------
        bounding_box_group: list[dict[np.ndarray, int]]
            The faces to analyze. A list of dictionaries, each with the following keys:
                bounding_box: np.ndarray
                    The bounding box of the face to analyze. The array contains four
                    values: [x1, y1, x2, y2]
                frame: int
                    The frame the bounding box of the face is associated with.
        frames: list[np.ndarray]
            The frames to analyze.

        Returns
        -------
        float
            The mouth movement of the faces across the frames.
        """
        mouth_movement = 0
        roi = Rect(0, 0, 0, 0)
        prev_mar = None
        mouth_movement = 0

        for bounding_box_data in bounding_box_group:
            # roi
            box = bounding_box_data["bounding_box"]
            x1, y1, x2, y2 = box[0], box[1], box[2], box[3]
            # sum all roi's, average after loop
            roi += Rect(x1, y1, x2 - x1, y2 - y1)
            frame = frames[bounding_box_data["frame"]]
            face = frame[y1: y2, x1: x2, :]

            # mouth movement
            mar = self._calc_mouth_aspect_ratio(face)
            if mar is None:
                continue
            if prev_mar is None:
                prev_mar = mar
                continue
            mouth_movement += abs(mar - prev_mar)
            prev_mar = mar

        return mouth_movement, roi / len(bounding_box_group)

    def _calc_mouth_aspect_ratio(self, face: np.ndarray) -> float:
        """
        Calculate the mouth aspect ratio using dlib shape predictor.

        Parameters
        ----------
        face_shape: np.ndarray
            Pytorch array of a face

        Returns
        -------
        mar: float
            The mouth aspect ratio.
        """
        results = self._face_mesher.process(face)
        if results.multi_face_landmarks is None:
            return None

        landmarks = []
        for landmark in results.multi_face_landmarks[0].landmark:
            landmarks.append([landmark.x, landmark.y])
        landmarks = np.array(landmarks)
        landmarks[:, 0] *= face.shape[1]
        landmarks[:, 1] *= face.shape[0]

        # inner lip
        upper_lip = landmarks[[95, 88, 178, 87, 14, 317, 402, 318, 324], :]
        lower_lip = landmarks[[191, 80, 81, 82, 13, 312, 311, 310, 415], :]
        avg_mouth_height = np.mean(np.abs(upper_lip - lower_lip))
        mouth_width = np.sum(np.abs(landmarks[[308], :] - landmarks[[78], :]))
        mar = avg_mouth_height / mouth_width

        return mar

    def _calc_crop(
        self,
        roi: Rect,
        resize_width: int,
        resize_height: int,
    ) -> Rect:
        """
        Calculate the crop given the ROI location.

        Parameters
        ----------
        roi: Rect
            The rectangle containing the region of interest (ROI).

        Returns
        -------
        Rect
            The crop rectangle.
        """
        roi_x_center = roi.x + roi.width // 2
        roi_y_center = roi.y + roi.height // 2
        crop = Rect(
            x=max(roi_x_center - (resize_width // 2), 0),
            y=max(roi_y_center - (resize_height // 2), 0),
            width=resize_width,
            height=resize_height,
        )
        return crop

    def _merge_identical_segments(
        self,
        segments: list[dict],
        video_file: VideoFile,
    ) -> list[dict]:
        """
        Merge identical segments that are next to each other.

        Parameters
        ----------
        segments: list[dict]
            The segments to merge.

        Returns
        -------
        list[dict]
            The merged segments.
        """
        idx = 0

        for _ in range(len(segments) - 1):
            cur_x = segments[idx]["x"]
            next_x = segments[idx + 1]["x"]
            x_diff = abs(cur_x - next_x)
            if (x_diff / video_file.get_width_pixels()) < 0.04:
                same_x = True
                segments[idx]["x"] = int((cur_x + next_x) // 2)
            else:
                same_x = False

            # y coordinate always zero for now, no need for fancy logic
            same_y = segments[idx]["y"] == segments[idx + 1]["y"]

            if same_x and same_y:
                segments[idx]["endTime"] = segments[idx + 1]["endTime"]
                segments = segments[:idx + 1] + segments[idx + 2:]
            else:
                idx += 1
        return segments

    def cleanup(self) -> None:
        """
        Remove the face detector from memory and explicity free up GPU memory.
        """
        del self._face_detector
        self._face_detector = None
        if torch.cuda.is_available():
            torch.cuda.empty_cache()