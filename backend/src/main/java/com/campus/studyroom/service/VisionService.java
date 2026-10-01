package com.campus.studyroom.service;

import com.baomidou.mybatisplus.core.conditions.query.LambdaQueryWrapper;
import com.campus.studyroom.common.BusinessException;
import com.campus.studyroom.entity.Reservation;
import com.campus.studyroom.entity.Seat;
import com.campus.studyroom.entity.SeatStatusLog;
import com.campus.studyroom.entity.Slot;
import com.campus.studyroom.mapper.ReservationMapper;
import com.campus.studyroom.mapper.SeatMapper;
import com.campus.studyroom.mapper.SeatStatusLogMapper;
import com.campus.studyroom.mapper.SlotMapper;
import lombok.RequiredArgsConstructor;
import lombok.extern.slf4j.Slf4j;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.core.ParameterizedTypeReference;
import org.springframework.http.HttpMethod;
import org.springframework.http.ResponseEntity;
import org.springframework.stereotype.Service;
import org.springframework.web.client.RestTemplate;

import java.math.BigDecimal;
import java.time.Duration;
import java.time.LocalDate;
import java.time.LocalDateTime;
import java.time.LocalTime;
import java.util.ArrayList;
import java.util.HashMap;
import java.util.List;
import java.util.Map;
import java.util.Set;
import java.util.concurrent.ConcurrentHashMap;

/**
 * 视觉识别联动服务：
 * 轮询视觉服务 /detect → 结合预约数据更新座位实时状态 → 状态翻转写检测日志
 * 同时维护"座位连续为空起始时间"，供违约/离座释放任务使用。
 */
@Slf4j
@Service
@RequiredArgsConstructor
public class VisionService {

    private final SeatMapper seatMapper;
    private final SlotMapper slotMapper;
    private final ReservationMapper reservationMapper;
    private final SeatStatusLogMapper seatStatusLogMapper;

    private final RestTemplate restTemplate = createRestTemplate();

    /** 带超时的 RestTemplate：避免视觉服务假死拖死轮询线程 */
    private static RestTemplate createRestTemplate() {
        org.springframework.http.client.SimpleClientHttpRequestFactory factory =
                new org.springframework.http.client.SimpleClientHttpRequestFactory();
        factory.setConnectTimeout(3000);
        factory.setReadTimeout(3000);
        return new RestTemplate(factory);
    }

    /** 座位最近一次检测为空的起始时间（内存态，重启后重建，可接受） */
    private final Map<Long, LocalDateTime> seatEmptySince = new ConcurrentHashMap<>();

    /** 座位疑似占座的持续占用起始时间（无有效预约但视觉占用） */
    private final Map<Long, LocalDateTime> seatOccupiedSince = new ConcurrentHashMap<>();

    /** 已触发过疑似占座告警的座位（每个占用周期只告警一次） */
    private final Set<Long> suspiciousWarned = ConcurrentHashMap.newKeySet();

    /**
     * 视觉状态快照：原子发布（可用性 + 本次实际覆盖的座位集合），
     * 避免可用性与覆盖集合分两次赋值导致的并发窗口误判
     */
    private static final class VisionSnapshot {
        final boolean available;
        final Set<Long> coveredSeatIds;

        VisionSnapshot(boolean available, Set<Long> coveredSeatIds) {
            this.available = available;
            this.coveredSeatIds = coveredSeatIds;
        }
    }

    /** 单座检测结果：占用标记 + 置信度 */
    private static final class DetectResult {
        final boolean occupied;
        final double confidence;

        DetectResult(boolean occupied, double confidence) {
            this.occupied = occupied;
            this.confidence = confidence;
        }
    }

    private volatile VisionSnapshot snapshot = new VisionSnapshot(false, Set.of());

    @Value("${studyroom.vision.base-url}")
    private String visionBaseUrl;

    @Value("${studyroom.vision.room-id:1}")
    private Long visionRoomId;

    @Value("${studyroom.rule.suspicious-after-minutes:10}")
    private int suspiciousAfterMinutes;

    /**
     * 轮询视觉服务并同步座位状态（由定时任务调用）
     */
    public void syncSeatStatus() {
        Map<Long, DetectResult> detect;
        try {
            detect = fetchDetect();
            // 原子发布：可用性 + 实际覆盖座位集合（results 覆盖全部已标定座位）
            snapshot = new VisionSnapshot(true, Set.copyOf(detect.keySet()));
        } catch (Exception e) {
            snapshot = new VisionSnapshot(false, Set.of());
            log.warn("视觉服务不可用: {}", e.getMessage());
            return;  // 降级：保留上次状态，预约功能不受影响
        }

        LocalDateTime now = LocalDateTime.now();
        List<Seat> seats = seatMapper.selectList(new LambdaQueryWrapper<Seat>()
                .eq(Seat::getRoomId, visionRoomId));
        for (Seat seat : seats) {
            if (seat.getStatus() == Seat.STATUS_DISABLED) {
                continue;
            }
            DetectResult dr = detect.get(seat.getId());
            // 视觉未覆盖该座位：跳过
            if (dr == null) {
                continue;
            }
            trackSuspicious(seat, dr.occupied, now);
            int newStatus = computeStatus(seat, dr.occupied, now);
            updateSeatStatus(seat, newStatus, dr.occupied, dr.confidence, now);
        }
    }

    /**
     * 计算座位应处的状态：预约数据优先，视觉兜底
     */
    private int computeStatus(Seat seat, boolean occupied, LocalDateTime now) {
        // 当前进行中的预约（now 落在 slot 时段内 且 状态为待签到/已签到）
        List<Reservation> active = reservationMapper.selectList(
                new LambdaQueryWrapper<Reservation>()
                        .eq(Reservation::getSeatId, seat.getId())
                        .eq(Reservation::getReserveDate, LocalDate.now())
                        .in(Reservation::getStatus, Reservation.STATUS_PENDING, Reservation.STATUS_SIGNED));
        for (Reservation r : active) {
            Slot slot = slotMapper.selectById(r.getSlotId());
            if (slot != null && !now.toLocalTime().isBefore(slot.getStartTime())
                    && now.toLocalTime().isBefore(slot.getEndTime())) {
                return r.getStatus() == Reservation.STATUS_SIGNED
                        ? Seat.STATUS_IN_USE : Seat.STATUS_RESERVED;
            }
        }
        return occupied ? Seat.STATUS_IN_USE : Seat.STATUS_FREE;
    }

    /**
     * 更新座位状态；翻转时写检测日志；维护 seatEmptySince
     */
    private void updateSeatStatus(Seat seat, int newStatus, boolean occupied, double confidence, LocalDateTime now) {
        if (seat.getStatus() != newStatus) {
            seat.setStatus(newStatus);
            seatMapper.updateById(seat);
            // 仅状态翻转时写日志（置信度取视觉服务真实值）
            SeatStatusLog statusLog = new SeatStatusLog();
            statusLog.setSeatId(seat.getId());
            statusLog.setOccupied(occupied ? 1 : 0);
            statusLog.setConfidence(BigDecimal.valueOf(confidence));
            statusLog.setDetector("mog2");
            statusLog.setDetectTime(now);
            seatStatusLogMapper.insert(statusLog);
        }
        // 维护连续为空时间（供违约/离座释放判定）
        if (!occupied) {
            seatEmptySince.computeIfAbsent(seat.getId(), k -> now);
        } else {
            seatEmptySince.remove(seat.getId());
        }
    }

    /**
     * 疑似占座跟踪：无有效预约但视觉持续占用 ≥ suspiciousAfterMinutes 时告警一次
     */
    private void trackSuspicious(Seat seat, boolean occupied, LocalDateTime now) {
        if (occupied) {
            if (!hasNoActiveReservation(seat.getId(), now)) {
                // 有有效预约：不是占座，重置计时
                seatOccupiedSince.remove(seat.getId());
                suspiciousWarned.remove(seat.getId());
                return;
            }
            seatOccupiedSince.computeIfAbsent(seat.getId(), k -> now);
            LocalDateTime since = seatOccupiedSince.get(seat.getId());
            if (since != null && Duration.between(since, now).toMinutes() >= suspiciousAfterMinutes
                    && suspiciousWarned.add(seat.getId())) {
                log.warn("疑似占座: 座位 {} 无有效预约且视觉占用持续 {} 分钟", seat.getSeatNo(), suspiciousAfterMinutes);
            }
        } else {
            seatOccupiedSince.remove(seat.getId());
            suspiciousWarned.remove(seat.getId());
        }
    }

    private boolean hasNoActiveReservation(Long seatId, LocalDateTime now) {
        List<Reservation> active = reservationMapper.selectList(
                new LambdaQueryWrapper<Reservation>()
                        .eq(Reservation::getSeatId, seatId)
                        .eq(Reservation::getReserveDate, LocalDate.now())
                        .in(Reservation::getStatus, Reservation.STATUS_PENDING, Reservation.STATUS_SIGNED));
        // 与 computeStatus 口径一致：仅"当前时段窗口内"的预约视为有效预约
        for (Reservation r : active) {
            Slot slot = slotMapper.selectById(r.getSlotId());
            if (slot != null && !now.toLocalTime().isBefore(slot.getStartTime())
                    && now.toLocalTime().isBefore(slot.getEndTime())) {
                return false;
            }
        }
        return true;
    }

    /**
     * 调用视觉服务 GET /detect?room_id=，返回 seatId -> occupied
     * 视觉服务按 seat_no 上报，此处映射为 seatId
     */
    private Map<Long, DetectResult> fetchDetect() {
        String url = visionBaseUrl + "/detect?room_id=" + visionRoomId;
        // 视觉服务返回顶层对象：{"room_id":..,"results":[{seat_no,occupied,confidence}]}
        ResponseEntity<Map<String, Object>> resp = restTemplate.exchange(
                url, HttpMethod.GET, null,
                new ParameterizedTypeReference<>() {});
        Map<String, Object> body = resp.getBody();
        if (body == null || body.containsKey("error")) {
            throw new BusinessException(502, "视觉服务响应异常: " + body);
        }
        @SuppressWarnings("unchecked")
        List<Map<String, Object>> list = (List<Map<String, Object>>) body.get("results");
        if (list == null) {
            return Map.of();
        }
        // seat_no -> id 映射
        Map<String, Long> seatNoToId = new HashMap<>();
        seatMapper.selectList(new LambdaQueryWrapper<Seat>().eq(Seat::getRoomId, visionRoomId))
                .forEach(s -> seatNoToId.put(s.getSeatNo(), s.getId()));

        Map<Long, DetectResult> result = new HashMap<>();
        for (Map<String, Object> item : list) {
            String seatNo = String.valueOf(item.get("seat_no"));
            Long seatId = seatNoToId.get(seatNo);
            if (seatId == null) {
                continue;
            }
            Object occ = item.get("occupied");
            boolean occupied = occ != null && (Boolean.TRUE.equals(occ)
                    || "true".equalsIgnoreCase(String.valueOf(occ)));
            double confidence = 0.9;  // 兜底默认值，优先取视觉服务真实置信度
            Object conf = item.get("confidence");
            if (conf instanceof Number n) {
                confidence = n.doubleValue();
            }
            result.put(seatId, new DetectResult(occupied, confidence));
        }
        return result;
    }

    /**
     * 视觉服务健康状态
     */
    public Map<String, Object> serviceStatus() {
        Map<String, Object> map = new HashMap<>();
        try {
            ResponseEntity<String> resp = restTemplate.getForEntity(visionBaseUrl + "/health", String.class);
            map.put("reachable", resp.getStatusCode().is2xxSuccessful());
            map.put("detail", resp.getBody());
        } catch (Exception e) {
            map.put("reachable", false);
            map.put("detail", e.getMessage());
        }
        return map;
    }

    /**
     * 触发视觉服务背景重置
     */
    public void resetBackground() {
        try {
            restTemplate.postForEntity(visionBaseUrl + "/reset", null, String.class);
        } catch (Exception e) {
            throw new BusinessException(500, "背景重置失败: " + e.getMessage());
        }
    }

    /**
     * 某座位连续为空时长（分钟），供离座释放判定
     */
    public long emptyMinutes(Long seatId, LocalDateTime now) {
        LocalDateTime since = seatEmptySince.get(seatId);
        if (since == null) {
            return 0;
        }
        return java.time.Duration.between(since, now).toMinutes();
    }

    /**
     * 视觉服务当前是否可用
     */
    public boolean isVisionAvailable() {
        return snapshot.available;
    }

    /**
     * 该座位是否在最近一次轮询中被视觉实际覆盖（未标定 ROI 的座位返回 false）
     */
    public boolean isSeatCovered(Long seatId) {
        return snapshot.coveredSeatIds.contains(seatId);
    }

    /**
     * 视觉是否已就绪并实际产出座位结果
     * （视觉服务刚启动/初始化背景时 results 为空，不应据此判定违约）
     */
    public boolean hasSeatCoverage() {
        return snapshot.available && !snapshot.coveredSeatIds.isEmpty();
    }

    /**
     * 前端轮询：某自习室座位实时视觉占用状态
     */
    public List<Map<String, Object>> visionStatus(Long roomId) {
        List<Map<String, Object>> result = new ArrayList<>();
        seatMapper.selectList(new LambdaQueryWrapper<Seat>().eq(Seat::getRoomId, roomId))
                .forEach(s -> {
                    Map<String, Object> m = new HashMap<>();
                    m.put("seatNo", s.getSeatNo());
                    m.put("status", s.getStatus());
                    m.put("visionOccupied", s.getStatus() == Seat.STATUS_IN_USE);
                    result.add(m);
                });
        return result;
    }
}
