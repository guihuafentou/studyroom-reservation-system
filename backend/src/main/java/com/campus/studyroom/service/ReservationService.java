package com.campus.studyroom.service;

import com.baomidou.mybatisplus.core.conditions.query.LambdaQueryWrapper;
import com.baomidou.mybatisplus.core.conditions.update.LambdaUpdateWrapper;
import com.campus.studyroom.common.BusinessException;
import com.campus.studyroom.dto.ReserveDTO;
import com.campus.studyroom.entity.Reservation;
import com.campus.studyroom.entity.Room;
import com.campus.studyroom.entity.Seat;
import com.campus.studyroom.entity.Slot;
import com.campus.studyroom.entity.User;
import com.campus.studyroom.mapper.ReservationMapper;
import com.campus.studyroom.mapper.RoomMapper;
import com.campus.studyroom.mapper.SeatMapper;
import com.campus.studyroom.mapper.SlotMapper;
import com.campus.studyroom.mapper.UserMapper;
import com.campus.studyroom.security.UserContext;
import com.campus.studyroom.vo.ReservationVO;
import lombok.RequiredArgsConstructor;
import lombok.extern.slf4j.Slf4j;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.stereotype.Service;
import org.springframework.transaction.annotation.Transactional;

import java.time.LocalDate;
import java.time.LocalDateTime;
import java.time.LocalTime;
import java.util.ArrayList;
import java.util.Comparator;
import java.util.HashMap;
import java.util.List;
import java.util.Map;

/**
 * 预约服务：创建（批量+事务+唯一索引兜底）/ 取消 / 签到 / 我的预约
 */
@Slf4j
@Service
@RequiredArgsConstructor
public class ReservationService {

    private final ReservationMapper reservationMapper;
    private final SeatMapper seatMapper;
    private final RoomMapper roomMapper;
    private final SlotMapper slotMapper;
    private final UserMapper userMapper;

    @Value("${studyroom.rule.cancel-before-minutes:30}")
    private int cancelBeforeMinutes;

    @Value("${studyroom.rule.sign-before-minutes:15}")
    private int signBeforeMinutes;

    @Value("${studyroom.rule.sign-after-minutes:15}")
    private int signAfterMinutes;

    @Value("${studyroom.rule.violate-limit:3}")
    private int violateLimit;

    @Value("${studyroom.rule.ban-days:7}")
    private int banDays;

    /**
     * 创建预约：一个座位 + 日期 + 1~N 个连续时段（每时段一条记录）
     * 并发安全：uk_seat_slot / uk_user_slot 唯一索引为最终防线，
     * 批量插入任一冲突则整体回滚。
     */
    @Transactional(rollbackFor = Exception.class)
    public List<Reservation> create(ReserveDTO dto) {
        Long userId = UserContext.getUserId();

        // 1. 用户禁约检查
        User user = userMapper.selectById(userId);
        if (user == null || user.getStatus() == 0) {
            throw BusinessException.forbidden("账号状态异常，无法预约");
        }
        if (user.getBanUntil() != null && user.getBanUntil().isAfter(LocalDateTime.now())) {
            throw BusinessException.forbidden("因违约累计达 " + violateLimit + " 次，预约权限暂停至 " + user.getBanUntil() + "，请届时再试");
        }

        // 2. 座位检查
        Seat seat = seatMapper.selectById(dto.getSeatId());
        if (seat == null) {
            throw BusinessException.notFound("座位不存在");
        }
        if (seat.getStatus() == Seat.STATUS_DISABLED) {
            throw BusinessException.badRequest("该座位已禁用");
        }
        Room room = roomMapper.selectById(seat.getRoomId());
        if (room == null || room.getStatus() == 0) {
            throw BusinessException.badRequest("自习室未开放");
        }

        // 3. 日期与时段校验
        LocalDate today = LocalDate.now();
        if (dto.getDate() == null || dto.getDate().isBefore(today)) {
            throw BusinessException.badRequest("不能预约过去的日期");
        }
        List<Slot> slots = slotMapper.selectList(new LambdaQueryWrapper<Slot>()
                .eq(Slot::getRoomId, seat.getRoomId())
                .in(Slot::getId, dto.getSlotIds()));
        if (slots.size() != dto.getSlotIds().size()) {
            throw BusinessException.badRequest("存在无效时段，请刷新后重试");
        }
        slots.sort(Comparator.comparing(Slot::getStartTime));

        // 3.1 时段须连续
        for (int i = 1; i < slots.size(); i++) {
            if (!slots.get(i).getStartTime().equals(slots.get(i - 1).getEndTime())) {
                throw BusinessException.badRequest("所选时段必须连续");
            }
        }
        // 3.2 时段在自习室开放时间内
        for (Slot s : slots) {
            if (s.getStartTime().isBefore(room.getOpenTime()) || s.getEndTime().isAfter(room.getCloseTime())) {
                throw BusinessException.badRequest("所选时段超出自习室开放时间");
            }
            // 今日不允许预约已开始的时段
            if (dto.getDate().equals(today) && s.getStartTime().isBefore(LocalTime.now())) {
                throw BusinessException.badRequest("所选时段已开始，无法预约");
            }
        }

        // 4. 批量插入
        List<Reservation> created = new ArrayList<>();
        for (Slot s : slots) {
            Reservation r = new Reservation();
            r.setUserId(userId);
            r.setRoomId(room.getId());
            r.setSeatId(seat.getId());
            r.setSlotId(s.getId());
            r.setReserveDate(dto.getDate());
            r.setStatus(Reservation.STATUS_PENDING);
            r.setCreateTime(LocalDateTime.now());
            reservationMapper.insert(r);
            created.add(r);
        }
        return created;
    }

    /**
     * 取消预约：本人 + 待签到 + 时段开始前 30 分钟以上（条件更新防竞态）
     */
    public void cancel(Long reservationId) {
        Reservation r = getOwnPending(reservationId);
        Slot slot = slotMapper.selectById(r.getSlotId());
        LocalDateTime slotStart = LocalDateTime.of(r.getReserveDate(), slot.getStartTime());
        if (LocalDateTime.now().isAfter(slotStart.minusMinutes(cancelBeforeMinutes))) {
            throw BusinessException.badRequest("时段开始前 " + cancelBeforeMinutes + " 分钟内不可取消");
        }
        int rows = reservationMapper.update(null,
                new LambdaUpdateWrapper<Reservation>()
                        .eq(Reservation::getId, reservationId)
                        .eq(Reservation::getStatus, Reservation.STATUS_PENDING)
                        .set(Reservation::getStatus, Reservation.STATUS_CANCELED));
        if (rows == 0) {
            throw BusinessException.conflict("预约状态已变更（可能已被系统判违约/取消），请刷新后重试");
        }
    }

    /**
     * 签到：本人 + 待签到 + 宽限期内（条件更新防竞态）
     */
    public ReservationVO sign(Long reservationId) {
        Reservation r = getOwnPending(reservationId);
        Slot slot = slotMapper.selectById(r.getSlotId());
        LocalDateTime slotStart = LocalDateTime.of(r.getReserveDate(), slot.getStartTime());
        LocalDateTime now = LocalDateTime.now();
        boolean inWindow = !now.isBefore(slotStart.minusMinutes(signBeforeMinutes))
                && !now.isAfter(slotStart.plusMinutes(signAfterMinutes));
        if (!inWindow) {
            throw BusinessException.badRequest("不在签到时间窗口内（时段开始前 " + signBeforeMinutes
                    + " 分钟至开始后 " + signAfterMinutes + " 分钟）");
        }
        int rows = reservationMapper.update(null,
                new LambdaUpdateWrapper<Reservation>()
                        .eq(Reservation::getId, reservationId)
                        .eq(Reservation::getStatus, Reservation.STATUS_PENDING)
                        .set(Reservation::getStatus, Reservation.STATUS_SIGNED)
                        .set(Reservation::getSignTime, now));
        if (rows == 0) {
            throw BusinessException.conflict("预约状态已变更（可能已被系统判违约），请刷新后重试");
        }
        r.setStatus(Reservation.STATUS_SIGNED);
        r.setSignTime(now);
        return toVO(r);
    }

    /**
     * 我的预约列表（按日期/时段倒序）
     */
    public List<ReservationVO> myReservations() {
        List<Reservation> list = reservationMapper.selectList(new LambdaQueryWrapper<Reservation>()
                .eq(Reservation::getUserId, UserContext.getUserId())
                .orderByDesc(Reservation::getReserveDate)
                .orderByAsc(Reservation::getSlotId));
        List<ReservationVO> vos = new ArrayList<>();
        for (Reservation r : list) {
            vos.add(toVO(r));
        }
        return vos;
    }

    private Reservation getOwnPending(Long reservationId) {
        Reservation r = reservationMapper.selectById(reservationId);
        if (r == null) {
            throw BusinessException.notFound("预约记录不存在");
        }
        if (!r.getUserId().equals(UserContext.getUserId())) {
            throw BusinessException.forbidden("无权操作他人的预约");
        }
        if (r.getStatus() != Reservation.STATUS_PENDING) {
            throw BusinessException.badRequest("当前状态不允许该操作");
        }
        return r;
    }

    /**
     * 组装视图：房间/座位/时段信息 + 可操作标记
     */
    public ReservationVO toVO(Reservation r) {
        ReservationVO vo = new ReservationVO();
        vo.setId(r.getId());
        vo.setReserveDate(r.getReserveDate());
        vo.setStatus(r.getStatus());
        vo.setStatusText(statusText(r.getStatus()));
        vo.setCreateTime(r.getCreateTime());
        vo.setSignTime(r.getSignTime());

        Room room = roomMapper.selectById(r.getRoomId());
        if (room != null) {
            vo.setRoomName(room.getName());
            vo.setBuilding(room.getBuilding());
        }
        Seat seat = seatMapper.selectById(r.getSeatId());
        if (seat != null) {
            vo.setSeatNo(seat.getSeatNo());
            vo.setSeatType(seat.getSeatType());
        }
        Slot slot = slotMapper.selectById(r.getSlotId());
        if (slot != null) {
            vo.setStartTime(slot.getStartTime());
            vo.setEndTime(slot.getEndTime());
        }
        if (r.getStatus() == Reservation.STATUS_PENDING && slot != null) {
            LocalDateTime slotStart = LocalDateTime.of(r.getReserveDate(), slot.getStartTime());
            vo.setCancelExpired(LocalDateTime.now().isAfter(slotStart.minusMinutes(cancelBeforeMinutes)));
            boolean inWindow = !LocalDateTime.now().isBefore(slotStart.minusMinutes(signBeforeMinutes))
                    && !LocalDateTime.now().isAfter(slotStart.plusMinutes(signAfterMinutes));
            vo.setSignable(inWindow);
        }
        return vo;
    }

    public static String statusText(int status) {
        return switch (status) {
            case Reservation.STATUS_PENDING -> "待签到";
            case Reservation.STATUS_SIGNED -> "已签到";
            case Reservation.STATUS_FINISHED -> "已完成";
            case Reservation.STATUS_CANCELED -> "已取消";
            case Reservation.STATUS_VIOLATED -> "违约";
            case Reservation.STATUS_EARLY_END -> "提前结束";
            default -> "未知";
        };
    }

    public Map<String, Object> statsBucket(LocalDate date) {
        Map<String, Object> map = new HashMap<>();
        map.put("date", date);
        return map;
    }
}
