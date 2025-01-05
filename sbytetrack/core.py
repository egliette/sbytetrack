from typing import List, Tuple, Dict

import numpy as np

from sbytetrack.utils import box_iou_batch
from sbytetrack import matching
from sbytetrack.kalman_filter import KalmanFilter
from sbytetrack.single_object_track import STrack, TrackState
from sbytetrack.utils import IdCounter


class BYTETrack:
    def __init__(
        self,
        n_classes: int,
        track_activation_threshold: float = 0.25,
        lost_track_buffer: int = 30,
        minimum_matching_threshold: float = 0.8,
        frame_rate: int = 30,
        minimum_consecutive_frames: int = 1,
    ):    
        """
        Initialize the ByteTrack objectt

        Parameters:
            n_classes (int): the total classes of object detection task
            track_activation_threshold (float): Detection confidence threshold
                for track activation. Increasing track_activation_threshold improves accuracy
                and stability but might miss true detections. Decreasing it increases
                completeness but risks introducing noise and instability.
            lost_track_buffer (int): Number of frames to buffer when a track is lost.
                Increasing lost_track_buffer enhances occlusion handling, significantly
                reducing the likelihood of track fragmentation or disappearance caused
                by brief detection gaps.
            minimum_matching_threshold (float): Threshold for matching tracks with detections.
                Increasing minimum_matching_threshold improves accuracy but risks fragmentation.
                Decreasing it improves completeness but risks false positives and drift.
            frame_rate (int): The frame rate of the video.
            minimum_consecutive_frames (int): Number of consecutive frames that an object must
                be tracked before it is considered a 'valid' track.
                Increasing minimum_consecutive_frames prevents the creation of accidental tracks from
                false detection or double detection, but risks missing shorter tracks.
        """  # noqa: E501 // docs
        self.track_activation_threshold = track_activation_threshold
        self.minimum_matching_threshold = minimum_matching_threshold

        self.frame_id = 0
        self.det_thresh = self.track_activation_threshold + 0.1
        self.max_time_lost = int(frame_rate / 30.0 * lost_track_buffer)
        self.minimum_consecutive_frames = minimum_consecutive_frames
        self.kalman_filter = KalmanFilter()
        self.shared_kalman = KalmanFilter()

        self.tracked_tracks: List[STrack] = []
        self.lost_tracks: List[STrack] = []
        self.removed_tracks: List[STrack] = []

        # Warning, possible bug: If you also set internal_id to start at 1,
        # all traces will be connected across objects.
        self.internal_id_counter = IdCounter()
        self.external_id_counter = IdCounter(start_id=1)
        self.n_classes = n_classes
        self.cls2tracked_tracks = {i: [] for i in range(n_classes)}


    def _cls_group(
            self, xyxy_array: np.array, conf_array: np.array, 
            cls_array: np.array,) -> Dict[int, Tuple]:
        """
            Group detections by their class IDs.

            Args:
                xyxy_array (np.array): Array of bounding box coordinates with shape (N, 4), 
                    where each row represents [x_min, y_min, x_max, y_max].
                conf_array (np.array): Array of confidence scores with shape (N,).
                cls_array (np.array): Array of class IDs with shape (N,).

            Returns:
                Dict[int, Tuple]: A dictionary where each key is a class ID (int) and 
                each value is a tuple containing:
                    - A list of bounding boxes (List[np.array]) corresponding to that class.
                    - A list of confidence scores (List[float]) corresponding to that class.
                    - A list of original detection orders (List[int]).
        """
        # cls2boxes[cls_i] = (cls_xyxy_array, cls_conf_array, box_order_list)
        cls2boxes = {cls_id: ([], [], []) for cls_id in range(self.n_classes)}
        for order, (xyxy, conf, cls_id) in enumerate(zip(xyxy_array, conf_array, cls_array)):
            cls2boxes[cls_id][0].append(xyxy)
            cls2boxes[cls_id][1].append(conf)
            cls2boxes[cls_id][2].append(order)

        return cls2boxes

    def update(
        self,
        xyxy_array: np.ndarray,
        conf_array: np.ndarray,
        cls_array: np.ndarray
    ) -> np.ndarray:
        """
        Updates the tracking system with new detections.

        Args:
            xyxy_array (np.ndarray): Array of bounding box coordinates in the format [x_min, y_min, x_max, y_max].
            conf_array (np.ndarray): Array of confidence scores corresponding to the bounding boxes.
            cls_array (np.ndarray): Array of class IDs corresponding to the bounding boxes.

        Returns:
            np.ndarray: Array of track IDs sorted by the original order of detections.
        """
        # Group detections by class into a dictionary: {class_id -> (boxes, confidences, original order)}.
        cls2boxes = self._cls_group(xyxy_array, conf_array, cls_array)

        track_id_array = np.array([], dtype=int)
        orig_order: List[int] = []
        current_frame_id = self.frame_id

        for cls_id in range(self.n_classes):
            cls_xyxy_array, cls_conf_array, cls_box_order_list = cls2boxes[cls_id]
            
            if len(cls_xyxy_array) < 1:
                continue

            orig_order += cls_box_order_list

            # Update and retrieve tracks for the current class.
            self.tracked_tracks = self.cls2tracked_tracks[cls_id]
            self.frame_id = current_frame_id
            cls_track_id_array = self.single_cls_update(cls_xyxy_array, cls_conf_array)

            self.cls2tracked_tracks[cls_id] = self.tracked_tracks
            track_id_array = np.concatenate((track_id_array, cls_track_id_array))

        self.frame_id = current_frame_id + 1
        # sort the track IDs to match the original detection order.

        return track_id_array[np.argsort(orig_order)]

    def single_cls_update(
        self,
        xyxy_array: np.ndarray,
        conf_array: np.ndarray
    ) -> np.ndarray:
        """
        Updates the tracking system for a single class of detections.

        Args:
            xyxy_array (np.ndarray): Array of bounding box coordinates in the format [x_min, y_min, x_max, y_max].
            conf_array (np.ndarray): Array of confidence scores corresponding to the bounding boxes.

        Returns:
            np.ndarray: Array of track IDs corresponding to the input detections. If no tracks are found, an empty array is returned.
        """
        if len(xyxy_array) * len(conf_array) == 0:
            return np.array([], dtype=int)

        cp_xyxy = np.array(xyxy_array).copy()
        cp_conf = np.array(conf_array).copy().reshape(-1, 1)
        tensors = np.hstack((cp_xyxy, cp_conf))
        tracks = self.update_with_tensors(tensors=tensors)

        if len(tracks) > 0:
            detection_bounding_boxes = np.asarray([det[:4] for det in tensors])
            track_bounding_boxes = np.asarray([track.tlbr for track in tracks])
            ious = box_iou_batch(detection_bounding_boxes, track_bounding_boxes)
            iou_costs = 1 - ious
            matches, _, _ = matching.linear_assignment(iou_costs, 0.5)
            track_id_array = np.full(len(cp_conf), -1, dtype=int)

            for i_detection, i_track in matches:
                track_id_array[i_detection] = int(tracks[i_track].external_track_id)

            return track_id_array
        else:
            return np.full(len(cp_conf), -1, dtype=int)


    def reset(self) -> None:
        """
        Resets the internal state of the ByteTrack tracker.

        This method clears the tracking data, including tracked, lost,
        and removed tracks, as well as resetting the frame counter. It's
        particularly useful when processing multiple videos sequentially,
        ensuring the tracker starts with a clean state for each new video.
        """
        self.frame_id = 0
        self.internal_id_counter.reset()
        self.external_id_counter.reset()
        self.tracked_tracks = []
        self.lost_tracks = []
        self.removed_tracks = []

    def update_with_tensors(self, tensors: np.ndarray) -> List[STrack]:
        """
        Updates the tracker with the provided tensors and returns the updated tracks.

        Parameters:
            tensors: The new tensors to update with.

        Returns:
            List[STrack]: Updated tracks.
        """
        self.frame_id += 1
        activated_starcks = []
        refind_stracks = []
        lost_stracks = []
        removed_stracks = []

        scores = tensors[:, 4]
        bboxes = tensors[:, :4]

        remain_inds = scores > self.track_activation_threshold
        inds_low = scores > 0.1
        inds_high = scores < self.track_activation_threshold

        inds_second = np.logical_and(inds_low, inds_high)
        dets_second = bboxes[inds_second]
        dets = bboxes[remain_inds]
        scores_keep = scores[remain_inds]
        scores_second = scores[inds_second]

        if len(dets) > 0:
            """Detections"""
            detections = [
                STrack(
                    STrack.tlbr_to_tlwh(tlbr),
                    score_keep,
                    self.minimum_consecutive_frames,
                    self.shared_kalman,
                    self.internal_id_counter,
                    self.external_id_counter,
                )
                for (tlbr, score_keep) in zip(dets, scores_keep)
            ]
        else:
            detections = []

        """ Add newly detected tracklets to tracked_stracks"""
        unconfirmed = []
        tracked_stracks = []  # type: list[STrack]

        for track in self.tracked_tracks:
            if not track.is_activated:
                unconfirmed.append(track)
            else:
                tracked_stracks.append(track)

        """ Step 2: First association, with high score detection boxes"""
        strack_pool = joint_tracks(tracked_stracks, self.lost_tracks)
        # Predict the current location with KF
        STrack.multi_predict(strack_pool, self.shared_kalman)
        dists = matching.iou_distance(strack_pool, detections)

        dists = matching.fuse_score(dists, detections)
        matches, u_track, u_detection = matching.linear_assignment(
            dists, thresh=self.minimum_matching_threshold
        )

        for itracked, idet in matches:
            track = strack_pool[itracked]
            det = detections[idet]
            if track.state == TrackState.Tracked:
                track.update(detections[idet], self.frame_id)
                activated_starcks.append(track)
            else:
                track.re_activate(det, self.frame_id)
                refind_stracks.append(track)

        """ Step 3: Second association, with low score detection boxes"""
        # association the untrack to the low score detections
        if len(dets_second) > 0:
            """Detections"""
            detections_second = [
                STrack(
                    STrack.tlbr_to_tlwh(tlbr),
                    score_second,
                    self.minimum_consecutive_frames,
                    self.shared_kalman,
                    self.internal_id_counter,
                    self.external_id_counter,
                )
                for (tlbr, score_second) in zip(dets_second, scores_second)
            ]
        else:
            detections_second = []
        r_tracked_stracks = [
            strack_pool[i]
            for i in u_track
            if strack_pool[i].state == TrackState.Tracked
        ]
        dists = matching.iou_distance(r_tracked_stracks, detections_second)
        matches, u_track, u_detection_second = matching.linear_assignment(
            dists, thresh=0.5
        )
        for itracked, idet in matches:
            track = r_tracked_stracks[itracked]
            det = detections_second[idet]
            if track.state == TrackState.Tracked:
                track.update(det, self.frame_id)
                activated_starcks.append(track)
            else:
                track.re_activate(det, self.frame_id)
                refind_stracks.append(track)

        for it in u_track:
            track = r_tracked_stracks[it]
            if not track.state == TrackState.Lost:
                track.state = TrackState.Lost
                lost_stracks.append(track)

        """Deal with unconfirmed tracks, usually tracks with only one beginning frame"""
        detections = [detections[i] for i in u_detection]
        dists = matching.iou_distance(unconfirmed, detections)

        dists = matching.fuse_score(dists, detections)
        matches, u_unconfirmed, u_detection = matching.linear_assignment(
            dists, thresh=0.7
        )
        for itracked, idet in matches:
            unconfirmed[itracked].update(detections[idet], self.frame_id)
            activated_starcks.append(unconfirmed[itracked])
        for it in u_unconfirmed:
            track = unconfirmed[it]
            track.state = TrackState.Removed
            removed_stracks.append(track)

        """ Step 4: Init new stracks"""
        for inew in u_detection:
            track = detections[inew]
            if track.score < self.det_thresh:
                continue
            track.activate(self.kalman_filter, self.frame_id)
            activated_starcks.append(track)
        """ Step 5: Update state"""
        for track in self.lost_tracks:
            if self.frame_id - track.frame_id > self.max_time_lost:
                track.state = TrackState.Removed
                removed_stracks.append(track)

        self.tracked_tracks = [
            t for t in self.tracked_tracks if t.state == TrackState.Tracked
        ]
        self.tracked_tracks = joint_tracks(self.tracked_tracks, activated_starcks)
        self.tracked_tracks = joint_tracks(self.tracked_tracks, refind_stracks)
        self.lost_tracks = sub_tracks(self.lost_tracks, self.tracked_tracks)
        self.lost_tracks.extend(lost_stracks)
        self.lost_tracks = sub_tracks(self.lost_tracks, self.removed_tracks)
        self.removed_tracks = removed_stracks
        self.tracked_tracks, self.lost_tracks = remove_duplicate_tracks(
            self.tracked_tracks, self.lost_tracks
        )
        output_stracks = [track for track in self.tracked_tracks if track.is_activated]

        return output_stracks


def joint_tracks(
    track_list_a: List[STrack], track_list_b: List[STrack]
) -> List[STrack]:
    """
    Joins two lists of tracks, ensuring that the resulting list does not
    contain tracks with duplicate internal_track_id values.

    Parameters:
        track_list_a: First list of tracks (with internal_track_id attribute).
        track_list_b: Second list of tracks (with internal_track_id attribute).

    Returns:
        Combined list of tracks from track_list_a and track_list_b
            without duplicate internal_track_id values.
    """
    seen_track_ids = set()
    result = []

    for track in track_list_a + track_list_b:
        if track.internal_track_id not in seen_track_ids:
            seen_track_ids.add(track.internal_track_id)
            result.append(track)

    return result


def sub_tracks(track_list_a: List[STrack], track_list_b: List[STrack]) -> List[int]:
    """
    Returns a list of tracks from track_list_a after removing any tracks
    that share the same internal_track_id with tracks in track_list_b.

    Parameters:
        track_list_a: List of tracks (with internal_track_id attribute).
        track_list_b: List of tracks (with internal_track_id attribute) to
            be subtracted from track_list_a.
    Returns:
        List of remaining tracks from track_list_a after subtraction.
    """
    tracks = {track.internal_track_id: track for track in track_list_a}
    track_ids_b = {track.internal_track_id for track in track_list_b}

    for track_id in track_ids_b:
        tracks.pop(track_id, None)

    return list(tracks.values())


def remove_duplicate_tracks(
    tracks_a: List[STrack], tracks_b: List[STrack]
) -> Tuple[List[STrack], List[STrack]]:
    pairwise_distance = matching.iou_distance(tracks_a, tracks_b)
    matching_pairs = np.where(pairwise_distance < 0.15)

    duplicates_a, duplicates_b = set(), set()
    for track_index_a, track_index_b in zip(*matching_pairs):
        time_a = tracks_a[track_index_a].frame_id - tracks_a[track_index_a].start_frame
        time_b = tracks_b[track_index_b].frame_id - tracks_b[track_index_b].start_frame
        if time_a > time_b:
            duplicates_b.add(track_index_b)
        else:
            duplicates_a.add(track_index_a)

    result_a = [
        track for index, track in enumerate(tracks_a) if index not in duplicates_a
    ]
    result_b = [
        track for index, track in enumerate(tracks_b) if index not in duplicates_b
    ]

    return result_a, result_b
