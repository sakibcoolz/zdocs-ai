package com.example;

import java.util.List;

/** Anything with an area. */
public interface Measurable {
    double area();
}

/** Anything that can be drawn. */
public interface Drawable extends Measurable {
    void draw();
}

/** Shared shape behaviour. */
public abstract class AbstractShape implements Drawable {
    protected final String name;
    private Logger logger;

    public AbstractShape(String name, Logger logger) {
        this.name = name;
        this.logger = logger;
    }

    public abstract double area();

    @Override
    public void draw() {
        logger.log(name);
    }
}

/** A rectangle. */
public class Rectangle extends AbstractShape implements Drawable {
    private double width;
    private double height;

    public Rectangle(double width, double height, Logger logger) {
        super("rectangle", logger);
        this.width = width;
        this.height = height;
    }

    @Override
    public double area() {
        return width * height;
    }
}
