package com.campus.studyroom.vo;

import lombok.Data;

import java.time.LocalDate;
import java.time.LocalDateTime;
import java.time.LocalTime;

/**
 * 预约记录视图
 */
@Data
public class ReservationVO {

    private Long id;
    private String roomName;
    private String building;
    private String seatNo;
    private Integer seatType;
    private LocalDate reserveDate;
    private LocalTime startTime;
    private LocalTime endTime;
    private Integer status;
    private String statusText;
    private LocalDateTime createTime;
    private LocalDateTime signTime;
    /** 是否已过取消截止时间 */
    private Boolean cancelExpired;
    /** 是否在签到宽限期内 */
    private Boolean signable;
}
