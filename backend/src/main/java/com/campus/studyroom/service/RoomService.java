package com.campus.studyroom.service;

import com.baomidou.mybatisplus.core.conditions.query.LambdaQueryWrapper;
import com.campus.studyroom.common.BusinessException;
import com.campus.studyroom.entity.Reservation;
import com.campus.studyroom.entity.Room;
import com.campus.studyroom.entity.Seat;
import com.campus.studyroom.entity.Slot;
import com.campus.studyroom.mapper.ReservationMapper;
import com.campus.studyroom.mapper.RoomMapper;
import com.campus.studyroom.mapper.SeatMapper;
import com.campus.studyroom.mapper.SlotMapper;
import com.campus.studyroom.vo.SeatVO;
import lombok.RequiredArgsConstructor;
import org.springframework.stereotype.Service;

import java.time.LocalDate;
import java.util.ArrayList;
import java.util.HashSet;
import java.util.List;
import java.util.Set;

/**
 * 自习室 / 座位 / 时段查询服务
 */
@Service
@RequiredArgsConstructor
public class RoomService {

    private final RoomMapper roomMapper;
    private final SeatMapper seatMapper;
    private final SlotMapper slotMapper;
    private final ReservationMapper reservationMapper;

    public List<Room> listRooms() {
        return roomMapper.selectList(new LambdaQueryWrapper<Room>()
                .eq(Room::getStatus, 1)
                .orderByAsc(Room::getBuilding, Room::getFloor));
    }

    public Room getRoom(Long id) {
        Room room = roomMapper.selectById(id);
        if (room == null) {
            throw BusinessException.notFound("自习室不存在");
        }
        return room;
    }

    public List<Slot> getSlots(Long roomId) {
        getRoom(roomId);
        return slotMapper.selectList(new LambdaQueryWrapper<Slot>()
                .eq(Slot::getRoomId, roomId)
                .orderByAsc(Slot::getStartTime));
    }

    /**
     * 座位列表：携带指定时段（slotId）的预约占用标记
     */
    public List<SeatVO> getSeats(Long roomId, LocalDate date, Long slotId, Long userId) {
        getRoom(roomId);
        List<Seat> seats = seatMapper.selectList(new LambdaQueryWrapper<Seat>()
                .eq(Seat::getRoomId, roomId)
                .orderByAsc(Seat::getRowNo, Seat::getColNo));
        List<Long> seatIds = seats.stream().map(Seat::getId).toList();

        // 该时段有效预约（待签到/已签到）
        Set<Long> reservedSeatIds = new HashSet<>();
        Set<Long> mySeatIds = new HashSet<>();
        if (date != null && slotId != null && !seatIds.isEmpty()) {
            List<Reservation> reservations = reservationMapper.selectList(
                    new LambdaQueryWrapper<Reservation>()
                            .in(Reservation::getSeatId, seatIds)
                            .eq(Reservation::getReserveDate, date)
                            .eq(Reservation::getSlotId, slotId)
                            .in(Reservation::getStatus, Reservation.STATUS_PENDING, Reservation.STATUS_SIGNED));
            for (Reservation r : reservations) {
                reservedSeatIds.add(r.getSeatId());
                if (r.getUserId().equals(userId)) {
                    mySeatIds.add(r.getSeatId());
                }
            }
        }

        List<SeatVO> result = new ArrayList<>();
        for (Seat s : seats) {
            SeatVO vo = new SeatVO();
            vo.setId(s.getId());
            vo.setRoomId(s.getRoomId());
            vo.setSeatNo(s.getSeatNo());
            vo.setRowNo(s.getRowNo());
            vo.setColNo(s.getColNo());
            vo.setSeatType(s.getSeatType());
            vo.setStatus(s.getStatus());
            vo.setReservedByMe(mySeatIds.contains(s.getId()));
            vo.setReservedByOther(reservedSeatIds.contains(s.getId()) && !mySeatIds.contains(s.getId()));
            vo.setVisionOccupied(Seat.STATUS_IN_USE == s.getStatus());
            result.add(vo);
        }
        return result;
    }
}
