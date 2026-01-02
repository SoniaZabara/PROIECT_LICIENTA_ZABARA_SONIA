%
( Program: DEMO-GCODE )
( Purpose: Demonstration program for parser testing )
( Units: Millimeters )
( Includes: linear moves, arcs, drilling cycles, parameters, expressions )

#1 = 10        (Part width)
#2 = 20        (Part height)
#3 = [#1/2]    (Midpoint X)
#4 = [#2/2]    (Midpoint Y)

(--- SAFE STARTUP BLOCK ---)
N10 G21 G17 G40 G49 G54 G80 G90
N20 G0 Z15.0
N30 T1 M6          (Tool change to T1)
N35 G0 X99 Y99

(--- SPINDLE ON ---)
N40 S12000 M3
N50 F1500

(--- MOVE TO START POSITION ---)
N60 G0 X999 Y0
N70 G1 Z-1.0 F800

(--- CUT RECTANGLE ---)
N80 G1 X#1
N90 G1 Y#2
N100 G1 X0
N110 G1 Y0

(--- GO TO CENTER ---)
N120 G0 Z10
N130 G0 X#3 Y#4

(--- CUT A CIRCLE AT CENTER ---)
N140 G1 Z-2.0 F600
N150 G2 X#3 Y#4 I5 J0     (Clockwise arc)
N160 G3 X#3 Y#4 R5         (Counterclockwise arc, radius format)

(--- DRILL PATTERN USING G83 PECK DRILL CYCLE ---)
N170 G0 Z10
N180 G0 X5 Y5
N190 G83 X5 Y5 Z-10 R1 Q2 F300
N200 G83 X15 Y5 Z-10 R1 Q2 F300
N210 G83 X15 Y15 Z-10 R1 Q2 F300
N220 G83 X5 Y15 Z-10 R1 Q2 F300
N230 G80                (Cancel drill cycle)

(--- A-AXIS ROTATION EXAMPLE ---)
N240 G0 Z15
N250 G0 A90

(--- PARAMETERIZED MOVE ---)
N260 G1 X[#1 - 2] Y[#2 - 2] F900

(--- ARC WITH EXPRESSION ---)
N270 G2 X[#3 + 10] Y[#4] I[5 * COS[30]] J[5 * SIN[30]]

(--- SPINDLE OFF ---)
N280 G0 Z20
N290 M5

(--- END PROGRAM ---)
N300 M30
%
