package com.campus.studyroom.mapper;

import com.baomidou.mybatisplus.core.mapper.BaseMapper;
import com.campus.studyroom.entity.Reservation;
import org.apache.ibatis.annotations.Param;
import org.apache.ibatis.annotations.Select;

import java.time.LocalDate;
import java.util.List;
import java.util.Map;

public interface ReservationMapper extends BaseMapper<Reservation> {

    /**
     * 按日期统计预约数（不含已取消）
     */
    @Select("SELECT reserve_date AS date, COUNT(*) AS count FROM reservation " +
            "WHERE reserve_date BETWEEN #{start} AND #{end} AND status != 3 " +
            "GROUP BY reserve_date ORDER BY reserve_date")
    List<Map<String, Object>> countByDate(@Param("start") LocalDate start, @Param("end") LocalDate end);

    /**
     * 按时段统计预约数（不含已取消），跨房间合并展示
     */
    @Select("SELECT CONCAT(s.start_time, '-', s.end_time) AS time_range, COUNT(*) AS count " +
            "FROM reservation r JOIN slot s ON r.slot_id = s.id " +
            "WHERE r.reserve_date BETWEEN #{start} AND #{end} AND r.status != 3 " +
            "GROUP BY s.id, s.start_time, s.end_time ORDER BY s.start_time")
    List<Map<String, Object>> countBySlot(@Param("start") LocalDate start, @Param("end") LocalDate end);
}
