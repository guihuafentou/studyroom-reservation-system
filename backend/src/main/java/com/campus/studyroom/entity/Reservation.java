package com.campus.studyroom.entity;

import com.baomidou.mybatisplus.annotation.IdType;
import com.baomidou.mybatisplus.annotation.TableId;
import com.baomidou.mybatisplus.annotation.TableName;
import lombok.Data;

import java.time.LocalDate;
import java.time.LocalDateTime;

/**
 * 预约记录表
 */
@Data
@TableName("reservation")
public class Reservation {

    /** 状态常量 */
    public static final int STATUS_PENDING = 0;    // 待签到
    public static final int STATUS_SIGNED = 1;     // 已签到
    public static final int STATUS_FINISHED = 2;   // 已完成（自然结束）
    public static final int STATUS_CANCELED = 3;   // 已取消
    public static final int STATUS_VIOLATED = 4;   // 违约
    public static final int STATUS_EARLY_END = 5;  // 提前结束（离座释放）

    @TableId(type = IdType.AUTO)
    private Long id;

    private Long userId;

    private Long roomId;

    private Long seatId;

    private Long slotId;

    private LocalDate reserveDate;

    private Integer status;

    private LocalDateTime createTime;

    private LocalDateTime signTime;
}
