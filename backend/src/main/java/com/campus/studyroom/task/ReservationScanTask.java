package com.campus.studyroom.task;

import com.baomidou.mybatisplus.core.conditions.query.LambdaQueryWrapper;
import com.baomidou.mybatisplus.core.conditions.update.LambdaUpdateWrapper;
import com.campus.studyroom.entity.Reservation;
import com.campus.studyroom.entity.Slot;
import com.campus.studyroom.entity.User;
import com.campus.studyroom.mapper.ReservationMapper;
import com.campus.studyroom.mapper.SlotMapper;
import com.campus.studyroom.mapper.UserMapper;
import com.campus.studyroom.service.VisionService;
import lombok.RequiredArgsConstructor;
import lombok.extern.slf4j.Slf4j;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.stereotype.Component;
import org.springframework.transaction.annotation.Transactional;

import java.time.LocalDate;
import java.time.LocalDateTime;
import java.util.List;

/**
 * 预约状态扫描定时任务（每分钟）：
 * 1) 超时未签到 → 违约（视觉确认空座 / 无视觉覆盖房间按规则直接违约）或释放不计违约（视觉服务故障）
 * 2) 已签到且时段已结束 → 已完成（状态机补全）
 * 3) 已签到但座位连续为空超阈值 → 提前结束并释放（仅视觉覆盖房间）
 * 4) 每日闭馆后重置视觉背景（23:05 执行）
 *
 * 并发安全：所有状态流转使用"条件更新"（WHERE id=? AND status=旧状态），
 * 与用户签到/取消操作互不覆盖（谁先提交谁生效）。
 */
@Slf4j
@Component
@RequiredArgsConstructor
public class ReservationScanTask {

    private final ReservationMapper reservationMapper;
    private final SlotMapper slotMapper;
    private final UserMapper userMapper;
    private final VisionService visionService;

    @Value("${studyroom.rule.violate-after-minutes:30}")
    private int violateAfterMinutes;

    @Value("${studyroom.rule.release-after-minutes:30}")
    private int releaseAfterMinutes;

    @Value("${studyroom.rule.violate-limit:3}")
    private int violateLimit;

    @Value("${studyroom.rule.ban-days:7}")
    private int banDays;

    @Value("${studyroom.vision.room-id:1}")
    private Long visionRoomId;

    @org.springframework.scheduling.annotation.Scheduled(cron = "0 * * * * ?")
    @Transactional(rollbackFor = Exception.class)
    public void scan() {
        LocalDateTime now = LocalDateTime.now();
        handleTimeoutViolations(now);
        handleFinished(now);
        handleEarlyRelease(now);
    }

    /**
     * 超时未签到：slotStart + violateAfterMinutes < now
     * - 视觉可用 & 视觉覆盖房间 & 座位空 → 违约
     * - 视觉可用 & 视觉覆盖房间 & 座位占用 → 学生可能在座，暂不处理
     * - 视觉可用 & 非视觉覆盖房间 → 按规则直接违约（无视觉兜底）
     * - 视觉服务故障 → 释放不计违约（技术故障不惩罚学生）
     */
    private void handleTimeoutViolations(LocalDateTime now) {
        List<Reservation> pending = reservationMapper.selectList(new LambdaQueryWrapper<Reservation>()
                .eq(Reservation::getStatus, Reservation.STATUS_PENDING)
                .le(Reservation::getReserveDate, LocalDate.now()));
        for (Reservation r : pending) {
            Slot slot = slotMapper.selectById(r.getSlotId());
            if (slot == null) {
                continue;
            }
            LocalDateTime slotStart = LocalDateTime.of(r.getReserveDate(), slot.getStartTime());
            if (!now.isAfter(slotStart.plusMinutes(violateAfterMinutes))) {
                continue;
            }
            boolean visionAvailable = visionService.isVisionAvailable();
            boolean visionCovered = r.getRoomId() != null && r.getRoomId().equals(visionRoomId);

            if (!visionAvailable) {
                // 视觉服务不可用 → 释放但不计违约
                conditionalUpdate(r.getId(), Reservation.STATUS_PENDING, Reservation.STATUS_CANCELED);
                log.info("视觉服务不可用，预约 {} 释放但不计违约", r.getId());
                continue;
            }
            if (visionCovered) {
                // 视觉服务刚启动/初始化背景时无座位结果：暂不处理，避免误判违约
                if (!visionService.hasSeatCoverage()) {
                    continue;
                }
                // 座位已标定且视觉确认非空 → 学生可能在座，暂不处理；其余（未标定/空座）→ 继续违约
                if (visionService.isSeatCovered(r.getSeatId())
                        && visionService.emptyMinutes(r.getSeatId(), now) == 0) {
                    continue;
                }
            }
            // 视觉确认空座，或房间无视觉覆盖（按规则执行）→ 违约
            if (conditionalUpdate(r.getId(), Reservation.STATUS_PENDING, Reservation.STATUS_VIOLATED)) {
                markViolation(r.getUserId());
            }
        }
    }

    /**
     * 已签到且时段已结束 → 已完成（含昨日遗留的已签到记录）
     */
    private void handleFinished(LocalDateTime now) {
        List<Reservation> signed = reservationMapper.selectList(new LambdaQueryWrapper<Reservation>()
                .eq(Reservation::getStatus, Reservation.STATUS_SIGNED)
                .le(Reservation::getReserveDate, LocalDate.now()));
        for (Reservation r : signed) {
            Slot slot = slotMapper.selectById(r.getSlotId());
            if (slot == null) {
                continue;
            }
            LocalDateTime slotEnd = LocalDateTime.of(r.getReserveDate(), slot.getEndTime());
            if (!now.isBefore(slotEnd)) {
                if (conditionalUpdate(r.getId(), Reservation.STATUS_SIGNED, Reservation.STATUS_FINISHED)) {
                    log.info("预约 {} 已完成（时段结束）", r.getId());
                }
            }
        }
    }

    /**
     * 离座释放（仅视觉覆盖房间）：已签到 + 视觉确认座位连续为空超阈值 → 提前结束
     */
    private void handleEarlyRelease(LocalDateTime now) {
        if (!visionService.isVisionAvailable()) {
            return;
        }
        List<Reservation> signed = reservationMapper.selectList(new LambdaQueryWrapper<Reservation>()
                .eq(Reservation::getStatus, Reservation.STATUS_SIGNED)
                .eq(Reservation::getReserveDate, LocalDate.now()));
        for (Reservation r : signed) {
            if (r.getRoomId() == null || !r.getRoomId().equals(visionRoomId)) {
                continue;
            }
            long emptyMinutes = visionService.emptyMinutes(r.getSeatId(), now);
            if (emptyMinutes >= releaseAfterMinutes) {
                if (conditionalUpdate(r.getId(), Reservation.STATUS_SIGNED, Reservation.STATUS_EARLY_END)) {
                    log.info("座位 {} 连续空 {} 分钟，预约 {} 提前结束并释放",
                            r.getSeatId(), emptyMinutes, r.getId());
                }
            }
        }
    }

    /**
     * 条件更新：仅当记录仍处于 expectedStatus 时流转到 newStatus，返回是否成功
     */
    private boolean conditionalUpdate(Long id, int expectedStatus, int newStatus) {
        int rows = reservationMapper.update(null,
                new LambdaUpdateWrapper<Reservation>()
                        .eq(Reservation::getId, id)
                        .eq(Reservation::getStatus, expectedStatus)
                        .set(Reservation::getStatus, newStatus));
        return rows > 0;
    }

    /**
     * 违约计数 + 达到阈值禁约
     */
    private void markViolation(Long userId) {
        User user = userMapper.selectById(userId);
        if (user == null) {
            return;
        }
        int count = (user.getViolationCount() == null ? 0 : user.getViolationCount()) + 1;
        user.setViolationCount(count);
        if (count >= violateLimit) {
            user.setBanUntil(LocalDateTime.now().plusDays(banDays));
            log.warn("用户 {} 违约达 {} 次，禁约至 {}", user.getUsername(), violateLimit, user.getBanUntil());
        }
        userMapper.updateById(user);
    }

    /**
     * 每日闭馆后重建视觉背景基准（23:05）
     */
    @org.springframework.scheduling.annotation.Scheduled(cron = "0 5 23 * * ?")
    public void resetBackgroundDaily() {
        try {
            visionService.resetBackground();
            log.info("每日视觉背景重置完成");
        } catch (Exception e) {
            log.warn("每日背景重置失败: {}", e.getMessage());
        }
    }
}
