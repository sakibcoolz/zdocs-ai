import { Logger } from './logger';

export interface Measurable {
  area(): number;
}

export interface Drawable extends Measurable {
  draw(): void;
}

export abstract class AbstractShape implements Drawable {
  protected readonly label: string;
  #hidden: number = 0;

  constructor(label: string, private readonly logger: Logger) {
    this.label = label;
  }

  abstract area(): number;

  draw(): void {
    this.logger.log(this.label);
  }
}

export class Rectangle extends AbstractShape implements Drawable {
  private box: BoundingBox;

  constructor(private width: number, private height: number, logger: Logger) {
    super('rectangle', logger);
    this.box = new BoundingBox(width, height);
  }

  area(): number {
    return this.width * this.height;
  }
}

export class BoundingBox {
  constructor(public width: number, public height: number) {}
}

export function totalArea(shapes: Measurable[]): number {
  return shapes.reduce((sum, shape) => sum + shape.area(), 0);
}

export type Maybe<T> = T | null;
